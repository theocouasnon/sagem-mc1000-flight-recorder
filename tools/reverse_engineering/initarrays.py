"""Recover static arrays initialised via RuntimeHelpers.InitializeArray + FieldRVA."""
import sys, struct
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ildasm import Reader
from dncil.clr.token import Token, StringToken

ELEM = {'Byte': (1, 'B'), 'SByte': (1, 'b'), 'UInt16': (2, 'H'), 'Int16': (2, 'h'),
        'UInt32': (4, 'I'), 'Int32': (4, 'i'), 'UInt64': (8, 'Q'), 'Int64': (8, 'q')}


def recover(path):
    r = Reader(path)
    # field name -> RVA
    rva_of = {}
    fields = r.md.Field.rows
    for row in r.md.FieldRva.rows:
        fi = row.Field.row_index
        rva_of[str(fields[fi - 1].Name)] = row.Rva

    out = {}
    for m in r.md.MethodDef.rows:
        if not m.Rva:
            continue
        try:
            b = r.body(m.Rva)
        except Exception:
            continue
        ins = b.instructions
        for i, x in enumerate(ins):
            if str(x.opcode) != 'stsfld':
                continue
            name = r.resolve(x.operand)
            # pattern: ldc.i4 <n> ; newarr <T> ; dup ; ldtoken <$$fld> ; call InitializeArray ; stsfld <name>
            if i < 5:
                continue
            w = ins[i - 5:i]
            if str(w[1].opcode) != 'newarr' or str(w[4].opcode) != 'call':
                continue
            if 'InitializeArray' not in str(r.resolve(w[4].operand)):
                continue
            etype = r.resolve(w[1].operand)
            blobname = r.resolve(w[3].operand)
            n_op = w[0].operand
            try:
                count = int(n_op) if n_op is not None else 0
            except Exception:
                count = 0
            if etype not in ELEM or blobname not in rva_of:
                continue
            size, fmt = ELEM[etype]
            data = r.pe.get_data(rva_of[blobname], count * size)
            vals = list(struct.unpack('<%d%s' % (count, fmt), data[:count * size]))
            out[name] = (etype, vals)
    return out


if __name__ == '__main__':
    arrays = recover(sys.argv[1])
    want = sys.argv[2:]
    for k, (t, v) in sorted(arrays.items()):
        if want and k not in want:
            continue
        print('%s : %s[%d]' % (k, t, len(v)))
        print('  ', ' '.join('%04X' % (x & 0xFFFF) if t.endswith('16') else str(x) for x in v))
        print()
