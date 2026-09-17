"""
Physical & Serial Bus Layer for ISO 9141-2 / ISO 14230 (K-Line).

Key Features:
- Half-Duplex Echo Stripping: K-Line ties TX and RX together; all transmitted
  bytes reflect immediately into the receiver and must be consumed and validated.
- ISO 14230 Fast-Init: 25ms LOW (Break), 25ms HIGH (Mark), StartCommunication (0x81).
- 5-Baud Slow-Init: Bit-banged 5-baud address byte (0x11), sync byte (0x55),
  key-byte handshake, and inverted address acknowledgment.
- High-performance response reading based on ISO 14230 length headers to avoid
  relying on serial timeout delays.
"""

import logging
import sys
import time
from typing import List, Optional, Tuple

import serial
import serial.tools.list_ports

from kwp2000 import (
    DEFAULT_SOURCE_TESTER,
    DEFAULT_TARGET_ECU,
    SID_START_COMMUNICATION,
    build_frame,
    calculate_checksum,
    parse_frame,
    KWPResponse,
    KWPProtocolError,
)

logger = logging.getLogger("sagem.serial")


class SerialBusError(Exception):
    """Base serial communication error."""
    pass


class EchoTimeoutError(SerialBusError):
    """Raised when the half-duplex echo was not fully received before timeout."""
    pass


class EchoMismatchError(SerialBusError):
    """Raised when the echoed bytes do not match transmitted bytes (bus collision/short)."""
    pass


class InitFailedError(SerialBusError):
    """Raised when both Fast-Init and Slow-Init fail to establish communication."""
    pass


def list_available_ports() -> List[str]:
    """Return a list of available serial port names on the host system."""
    ports = serial.tools.list_ports.comports()
    return [p.device for p in ports]


def auto_detect_ftdi_port() -> Optional[str]:
    """
    Search for connected FTDI FT232 or USB-Serial devices.
    Returns the port name if found, else None.
    """
    for port in serial.tools.list_ports.comports():
        desc = (port.description or "").lower()
        hwid = (port.hwid or "").lower()
        mfg = (port.manufacturer or "").lower()
        if "ftdi" in desc or "ftdi" in hwid or "ftdi" in mfg:
            return port.device
        if "usb serial" in desc or "usb-serial" in desc or "ch340" in desc:
            return port.device
    return None


class KLineSerialBus:
    """
    Manages half-duplex single-wire K-Line serial communication.
    Handles echo stripping, fast-init, and 5-baud slow-init.
    """

    def __init__(
        self,
        port: str,
        baudrate: int = 10400,
        timeout: float = 0.2,
        target_ecu: int = DEFAULT_TARGET_ECU,
        source_tester: int = DEFAULT_SOURCE_TESTER,
    ):
        self.port_name = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.target_ecu = target_ecu
        self.source_tester = source_tester
        self.ser: Optional[serial.Serial] = None
        self.is_connected = False
        self.protocol = "KWP2000"
        # Mode 01 PIDs the ECU actually answers, filled in by discover_supported_pids().
        # Empty set means "not probed yet" -- callers should not treat it as "nothing supported".
        self.supported_pids: set[int] = set()

    def open(self) -> None:
        """Open the serial port with 8N1 configuration."""
        if self.ser and self.ser.is_open:
            return

        try:
            self.ser = serial.Serial(
                port=self.port_name,
                baudrate=self.baudrate,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=self.timeout,
                write_timeout=self.timeout,
            )
            # Ensure buffers are empty
            self.ser.reset_input_buffer()
            self.ser.reset_output_buffer()
            logger.info("Opened K-Line serial port %s at %d baud", self.port_name, self.baudrate)
        except Exception as e:
            raise SerialBusError(f"Failed to open port {self.port_name}: {e}") from e

    def close(self) -> None:
        """Close the serial port."""
        if self.ser and self.ser.is_open:
            try:
                self.ser.close()
            except Exception:
                pass
        self.ser = None
        self.is_connected = False
        logger.info("Closed K-Line serial port %s", self.port_name)

    def send_raw_with_echo_strip(self, tx_bytes: bytes) -> None:
        """
        Transmit raw bytes on the single-wire K-Line and immediately strip the
        hardware echo from the RX buffer. Verifies that the echo matches tx_bytes.
        """
        if not self.ser or not self.ser.is_open:
            raise SerialBusError("Serial port is not open")

        # Flush stale input before transmission
        self.ser.reset_input_buffer()

        self.ser.write(tx_bytes)
        self.ser.flush()

        # The transceiver echoes all transmitted bytes onto RX
        echo = self.ser.read(len(tx_bytes))
        if len(echo) < len(tx_bytes):
            raise EchoTimeoutError(
                f"Echo timeout: expected {len(tx_bytes)} bytes, received {len(echo)} bytes ({echo.hex()})"
            )

        if echo != tx_bytes:
            raise EchoMismatchError(
                f"Echo mismatch / bus collision: sent {tx_bytes.hex()}, echoed {echo.hex()}"
            )

    def read_frame(self, max_timeout: Optional[float] = None) -> bytes:
        """
        Read a single ISO 14230 response frame from the ECU.
        Uses header decoding so it only reads the exact number of bytes required,
        preventing waiting for serial read timeouts.
        """
        if not self.ser or not self.ser.is_open:
            raise SerialBusError("Serial port is not open")

        orig_timeout = self.ser.timeout
        if max_timeout is not None:
            self.ser.timeout = max_timeout

        try:
            # Read format byte
            fmt_raw = self.ser.read(1)
            if not fmt_raw:
                raise SerialBusError("Timeout waiting for response frame start")

            fmt = fmt_raw[0]

            # Read target and source
            addr = self.ser.read(2)
            if len(addr) < 2:
                raise SerialBusError(f"Incomplete address header: {addr.hex()}")

            # Calculate expected payload length
            # If bit 7..6 are 10 (0x80), bits 5..0 hold length (1..63)
            length_in_fmt = fmt & 0x3F
            if length_in_fmt > 0:
                payload_len = length_in_fmt
                # Remaining to read: payload_len + 1 byte checksum
                remaining_len = payload_len + 1
            else:
                # Length byte follows address
                len_raw = self.ser.read(1)
                if not len_raw:
                    raise SerialBusError("Timeout reading extended length byte")
                payload_len = len_raw[0]
                remaining_len = payload_len + 1
                fmt_raw += len_raw

            rest = self.ser.read(remaining_len)
            if len(rest) < remaining_len:
                raise SerialBusError(
                    f"Truncated frame: expected {remaining_len} bytes, got {len(rest)} ({rest.hex()})"
                )

            full_frame = fmt_raw + addr + rest
            return full_frame

        finally:
            if max_timeout is not None:
                self.ser.timeout = orig_timeout

    def send_and_receive(
        self,
        service_id: int,
        data: bytes | bytearray | List[int] = b"",
        max_timeout: Optional[float] = None,
    ) -> KWPResponse:
        """
        Build frame, send with half-duplex echo stripping, and read/parse response.
        """
        req_frame = build_frame(
            service_id=service_id,
            data=data,
            target=self.target_ecu,
            source=self.source_tester,
        )
        self.send_raw_with_echo_strip(req_frame)
        rx_raw = self.read_frame(max_timeout=max_timeout)
        response = parse_frame(
            rx_raw,
            expected_target=self.source_tester,
            expected_source=self.target_ecu,
        )
        return response

    def fast_init(self) -> bool:
        """
        Execute ISO 14230 Fast-Initialization sequence:
        1. Hold TX LOW (Break) for 25ms
        2. Hold TX HIGH (Mark) for 25ms
        3. Transmit StartCommunication request: 0x81 0x11 0xF1 0x81 0x04
        4. Strip echo and await response 0xC1
        """
        logger.info("Initiating ISO 14230 Fast-Init on %s...", self.port_name)
        if not self.ser or not self.ser.is_open:
            self.open()

        # Step 1: 25ms LOW (Break)
        self.ser.break_condition = True
        time.sleep(0.025)

        # Step 2: 25ms HIGH (Mark)
        self.ser.break_condition = False
        time.sleep(0.025)

        # Step 3: Send StartCommunication request (0x81 0x11 0xF1 0x81 0x04)
        start_comm = build_frame(
            service_id=SID_START_COMMUNICATION,
            data=b"",
            target=self.target_ecu,
            source=self.source_tester,
        )
        try:
            self.send_raw_with_echo_strip(start_comm)
            # Response timeout for initial wakeup (P2 max: up to 300ms)
            rx_raw = self.read_frame(max_timeout=0.35)
            response = parse_frame(rx_raw)
            if response.is_positive and response.service_id == SID_START_COMMUNICATION:
                logger.info("Fast-Init successful! ECU Key Bytes: %s", response.data.hex())
                self.is_connected = True
                return True
            else:
                logger.warning(
                    "Fast-Init received negative/unexpected response: %s",
                    response.nrc_description,
                )
                return False
        except Exception as e:
            logger.warning("Fast-Init failed: %s", e)
            return False

    def slow_init_5baud(self, address: Optional[int] = None) -> bool:
        """
        Execute 5-Baud Slow-Initialization sequence:
        1. Hold line high for >= 0.5s idle.
        2. Transmit address byte at 5 baud (200ms per bit: start=0, 8 bits LSB, stop=1).
        3. Listen for sync byte 0x55 and key bytes KB1 & KB2.
        4. Wait W4 (35ms).
        5. Invert KB2 and transmit (~KB2 & 0xFF).
        6. Await inverted address ACK (~address & 0xFF).
        """
        target_addr = address if address is not None else self.target_ecu
        logger.info("Attempting 5-Baud Slow-Init (target=0x%02X) on %s...", target_addr, self.port_name)
        if not self.ser or not self.ser.is_open:
            self.open()

        # Hold line high (idle) for at least 0.5s to ensure clean sync
        self.ser.break_condition = False
        time.sleep(0.500)
        self.ser.reset_input_buffer()
        self.ser.reset_output_buffer()

        # Bit-bang 5 baud (0.2s per bit) using break condition
        bits = [0] + [(target_addr >> i) & 1 for i in range(8)] + [1]
        bit_time = 0.200

        t_start = time.perf_counter()
        for i, bit in enumerate(bits):
            self.ser.break_condition = (bit == 0)
            target_time = t_start + (i + 1) * bit_time
            sleep_duration = target_time - time.perf_counter()
            if sleep_duration > 0:
                time.sleep(sleep_duration)

        # Line idle high
        self.ser.break_condition = False

        # Read back sync byte (0x55) and key bytes (KB1, KB2)
        # Discard any break echo noise
        t_wait = time.time() + 0.8
        sync_found = False
        key_bytes = bytearray()

        while time.time() < t_wait and len(key_bytes) < 2:
            b = self.ser.read(getattr(self.ser, "in_waiting", 0) or 1)
            if not b:
                time.sleep(0.002)
                continue
            for val in b:
                if not sync_found:
                    if val == 0x55:
                        sync_found = True
                else:
                    key_bytes.append(val)
                    if len(key_bytes) == 2:
                        break

        if not sync_found or len(key_bytes) < 2:
            logger.warning("Slow-init: Expected sync byte 0x55 & key bytes, got %s", key_bytes.hex() if key_bytes else "None")
            return False

        kb1, kb2 = key_bytes[0], key_bytes[1]
        logger.info("Slow-init: Received sync 0x55, KB1=0x%02X, KB2=0x%02X", kb1, kb2)

        # Wait W4 time (35ms)
        time.sleep(0.035)

        # Tester inverts KB2 and sends it back (~KB2 & 0xFF)
        inv_kb2 = bytes([(~kb2) & 0xFF])
        self.ser.write(inv_kb2)
        self.ser.flush()

        # Read the ECU's inverted-address ACK (~target_addr & 0xFF).
        #
        # Two things made this fragile before. The line is half-duplex, so our
        # own inverted-KB2 byte comes back as an echo first -- but not always,
        # since some adapters strip it -- and the old code assumed the ACK sat at
        # exactly index 1. It also gave up after 400ms. On a warm ECU that has
        # just finished a previous session the ACK can arrive later than that,
        # which showed up as "Expected ack 0xCC, got f7": f7 is the echo of our
        # own transmission, and the real ACK simply had not arrived yet.
        #
        # Now: scan for the ACK anywhere in what comes back, and wait longer.
        expected_ack = (~target_addr) & 0xFF
        echo = inv_kb2[0]
        t_ack = time.time() + 1.0
        ack_rx = bytearray()
        while time.time() < t_ack:
            b = self.ser.read(getattr(self.ser, "in_waiting", 0) or 1)
            if b:
                ack_rx.extend(b)
                if expected_ack in ack_rx:
                    break
            else:
                time.sleep(0.002)

        if expected_ack not in ack_rx:
            stray = bytes(x for x in ack_rx if x != echo)
            logger.warning(
                "Slow-init: Expected ack 0x%02X, got %s%s",
                expected_ack,
                ack_rx.hex() if ack_rx else "None",
                " (echo only -- ECU never answered)" if ack_rx and not stray else "",
            )
            return False

        logger.info("5-Baud Slow-Init handshake successful (ACK 0x%02X)!", expected_ack)
        self.is_connected = True
        self.protocol = "ISO9141" if target_addr == 0x33 else "KWP2000"
        return True

    def send_iso9141_request(self, mode: int, pid: Optional[int] = None) -> bytes:
        """
        Send an ISO 9141-2 OBD request with half-duplex echo stripping.
        Frame: [0x68, 0x6A, 0xF1, mode, (pid), checksum]
        """
        if pid is not None:
            body = [0x68, 0x6A, 0xF1, mode, pid]
        else:
            body = [0x68, 0x6A, 0xF1, mode]
        csum = sum(body) & 0xFF
        packet = bytes(body + [csum])
        self.send_raw_with_echo_strip(packet)
        return packet

    def read_iso9141_response(self, expected_len: int = 8, timeout: float = 0.08) -> bytes:
        """Read and validate an ISO 9141-2 response frame."""
        if not self.ser or not self.ser.is_open:
            raise SerialBusError("Serial port is not open")

        t_limit = time.time() + timeout
        resp = bytearray()
        while time.time() < t_limit and len(resp) < expected_len:
            b = self.ser.read(expected_len - len(resp))
            if b:
                resp.extend(b)
            else:
                time.sleep(0.001)

        if len(resp) < 4:
            return bytes()

        if sum(resp[:-1]) & 0xFF != resp[-1]:
            logger.debug("ISO 9141 Checksum mismatch: %s", resp.hex())
            return bytes()

        return bytes(resp)

    def query_iso9141(self, mode: int, pid: Optional[int] = None, expected_len: int = 8, timeout: float = 0.08) -> bytes:
        """Send ISO 9141 request and return validated response."""
        self.send_iso9141_request(mode, pid)
        return self.read_iso9141_response(expected_len=expected_len, timeout=timeout)

    def discover_supported_pids(self, max_banks: int = 4) -> set[int]:
        """
        Probe which Mode 01 PIDs this ECU actually answers, instead of guessing.

        PID 0x00 returns a 4-byte bitmask for PIDs 0x01-0x20, where bit 31 (MSB of
        the first byte) is PID 0x01 and the LSB of the last byte is PID 0x20. If
        PID 0x20 is itself flagged as supported, the next bank (0x20 -> 0x21-0x40)
        can be queried the same way, and so on.

        Returns the set of supported PID numbers, and caches it on self.supported_pids.
        """
        found: set[int] = set()
        bank_pid = 0x00

        for _ in range(max_banks):
            resp = self.query_iso9141(mode=0x01, pid=bank_pid, expected_len=10, timeout=0.15)
            if len(resp) < 9 or resp[3] != 0x41 or resp[4] != bank_pid:
                logger.debug("PID bank 0x%02X not answered (%s)", bank_pid, resp.hex() or "no data")
                break

            mask = int.from_bytes(resp[5:9], "big")
            for bit in range(32):
                if mask & (1 << (31 - bit)):
                    found.add(bank_pid + bit + 1)

            next_bank = bank_pid + 0x20
            if next_bank not in found:
                break
            bank_pid = next_bank

        self.supported_pids = found
        if found:
            logger.info(
                "ECU reports %d supported Mode 01 PIDs: %s",
                len(found),
                " ".join(f"{p:02X}" for p in sorted(found)),
            )
        else:
            logger.warning(
                "ECU did not answer PID 0x00 support bitmask -- falling back to probing "
                "each PID individually"
            )
        return found

    def probe_pids_individually(self, candidates: List[int]) -> set[int]:
        """
        Fallback discovery for ECUs that do not implement the PID 0x00 bitmask:
        ask for each candidate PID once and keep the ones that answer.
        Slow (~80ms each), so this only runs at connect time.
        """
        found: set[int] = set()
        for pid in candidates:
            resp = self.query_iso9141(mode=0x01, pid=pid, expected_len=7, timeout=0.12)
            if len(resp) >= 6 and resp[3] == 0x41 and resp[4] == pid:
                found.add(pid)
        self.supported_pids = found
        logger.info(
            "Individual PID probe found %d of %d candidates: %s",
            len(found),
            len(candidates),
            " ".join(f"{p:02X}" for p in sorted(found)) or "(none)",
        )
        return found

    def initialize(self) -> bool:
        """
        High-level initialization:
        1. On physical ports: try 5-baud Slow-Init with address 0x33 (Caponord Sagem MC1000).
        2. Fallback: try Fast-Init with address 0x11 (KWP2000 / Mock).
        3. Fallback: try 5-baud Slow-Init with address 0x11.
        """
        if self.port_name != "MOCK":
            logger.info("Physical hardware on %s: testing Caponord Sagem MC1000 Slow-Init (0x33)...", self.port_name)
            # Retry: the 5-baud handshake is timing sensitive and fails
            # intermittently, especially soon after a previous session when the
            # ECU has not yet dropped back to idle. A single failed attempt is
            # not evidence of a wiring problem, and treating it as one wastes
            # time chasing the wrong thing.
            for attempt in range(1, 4):
                try:
                    if self.slow_init_5baud(address=0x33):
                        return True
                except Exception as e:
                    logger.debug("Slow-init 0x33 attempt %d error: %s", attempt, e)
                if attempt < 3:
                    logger.info("Slow-init attempt %d failed; retrying in 2s...", attempt)
                    time.sleep(2.0)

        # Fast-Init fallback (0x11)
        try:
            if self.fast_init():
                self.protocol = "KWP2000"
                return True
        except Exception as e:
            logger.debug("Fast-init attempt error: %s", e)

        # Slow-Init fallback (0x11)
        logger.info("Attempting Slow-Init (0x11)...")
        time.sleep(0.5)
        if self.slow_init_5baud(address=0x11):
            self.protocol = "KWP2000"
            return True

        raise InitFailedError(
            f"Failed to initialize ECU communication on {self.port_name} via Slow-Init (0x33), Fast-Init, and Slow-Init (0x11)."
        )
