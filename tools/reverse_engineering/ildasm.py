"""Minimal .NET IL disassembler for TuneECU, built on dnfile + dncil."""
import sys, struct
import dnfile
from dncil.cil.body import CilMethodBody
from dncil.cil.body.reader import CilMethodBodyReaderBase
from dncil.cil.error import MethodBodyFormatError
from dncil.clr.token import Token, StringToken, InvalidToken


class Reader:
    def __init__(self, path):
        self.pe = dnfile.dnPE(path)
        self.md = self.pe.net.mdtables

    def offset_from_rva(self, rva):
        return self.pe.get_offset_from_rva(rva)

    def body(self, rva):
        # Read to the end of the containing section: large method bodies
        # (notably .cctor array initialisers) overrun any fixed window.
        for sec in self.pe.sections:
            start = sec.VirtualAddress
            end = start + max(sec.Misc_VirtualSize, sec.SizeOfRawData)
            if start <= rva < end:
                data = self.pe.get_data(rva, end - rva)
                break
        else:
            data = self.pe.get_data(rva, 0x2000)
        return CilMethodBody(_R(data))

    def resolve(self, tok):
        if isinstance(tok, StringToken):
            try:
                return '"%s"' % self.pe.net.user_strings.get_us(tok.rid).value
            except Exception:
                return "<str?>"
        t, rid = tok.table, tok.rid
        try:
            if t == 6:   # MethodDef
                return str(self.md.MethodDef.rows[rid - 1].Name)
            if t == 10:  # MemberRef
                row = self.md.MemberRef.rows[rid - 1]
                return str(row.Name)
            if t == 4:   # Field
                return str(self.md.Field.rows[rid - 1].Name)
            if t == 1:   # TypeRef
                return str(self.md.TypeRef.rows[rid - 1].TypeName)
            if t == 2:   # TypeDef
                return str(self.md.TypeDef.rows[rid - 1].TypeName)
        except Exception:
            pass
        return "tok(%d:%d)" % (t, rid)


class _R(CilMethodBodyReaderBase):
    """Byte reader interface dncil expects."""
    def __init__(self, data):
        self.data = data
        self.offset = 0

    def read(self, n):
        b = self.data[self.offset:self.offset + n]
        self.offset += n
        return b

    def tell(self):
        return self.offset

    def seek(self, off):
        self.offset = off
        return self.offset


def dump(path, names, maxins=400):
    r = Reader(path)
    rows = r.md.MethodDef.rows
    for i, m in enumerate(rows):
        nm = str(m.Name)
        if nm not in names:
            continue
        print("=" * 78)
        print("%s   (rva 0x%x)" % (nm, m.Rva))
        print("=" * 78)
        try:
            b = r.body(m.Rva)
        except Exception as e:
            print("  <failed: %s>" % e)
            continue
        for ins in b.instructions[:maxins]:
            op = str(ins.opcode)
            operand = ""
            if ins.operand is not None:
                if isinstance(ins.operand, (Token, StringToken)):
                    operand = r.resolve(ins.operand)
                else:
                    operand = str(ins.operand)
            print("  IL_%04X  %-12s %s" % (ins.offset - b.offset, op, operand))
        print()


if __name__ == "__main__":
    path = sys.argv[1]
    names = set(sys.argv[2:])
    dump(path, names)
