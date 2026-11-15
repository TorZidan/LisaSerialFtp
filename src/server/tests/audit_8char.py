#!/usr/bin/env python3
"""Audit LisaSerialFtpClient.text for 8-character identifier-prefix collisions.

Lisa Pascal truncates identifiers to 8 characters and folds case, so
two identifiers with the same 8-char prefix (case-insensitive) are the
same identifier.  Collisions that matter:
  * two program-scope identifiers in LisaSerialFtpClient.text (globals, proc/fn
    names, types, consts)
  * a program-scope identifier vs an identifier imported from the
    syscall unit (its INTERFACE section)
Local (procedure-body) declarations shadow program scope legally and
are reported only as informational.
"""
import re, sys, collections

PASCAL_KEYWORDS = {
    'and','array','begin','case','const','div','do','downto','else','end',
    'file','for','function','goto','if','in','label','mod','nil','not',
    'of','or','packed','procedure','program','record','repeat','set',
    'then','to','type','until','uses','var','while','with','xor',
    'intrinsic','interface','implementation','requires','unit','abs','chr',
    'copy','delete','dispose','exit','false','get','high','inc','insert',
    'length','low','new','odd','ord','pred','put','read','readln','reset',
    'rewrite','round','seek','setlength','setord','succ','true','val',
    'write','writeln','eof','eoln','truncate','close','open','append',
    'blockread','blockwrite','assign','rename','pack','unpack',
}

def strip_comments_strings(src):
    """Single-pass removal of {..} and (*..*) comments and '...' strings.
    Newlines inside removed regions are preserved so line-anchored regexes
    still see the original line structure."""
    out = []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c == "'":
            j = i + 1
            while j < n:
                if src[j] == "'":
                    if j + 1 < n and src[j + 1] == "'":
                        j += 2
                        continue
                    break
                j += 1
            out.append("''")
            i = min(j + 1, n)
        elif c == '{' or (c == '(' and i + 1 < n and src[i + 1] == '*'):
            if c == '{':
                j = src.find('}', i + 1)
                j = n if j == -1 else j + 1
            else:
                j = src.find('*)', i + 2)
                j = n if j == -1 else j + 2
            out.append('\n' * src[i:j].count('\n'))
            i = j
        else:
            out.append(c)
            i += 1
    return ''.join(out)

def decl_names(section, header_kw):
    """Extract declared names from a Pascal declaration section.
    header_kw: e.g. 'PROCEDURE' or 'FUNCTION' (optional)."""
    names = []
    # constants: NAME =
    for m in re.finditer(r'^\s*([A-Za-z]\w*)\s*=', section, flags=re.M):
        names.append(m.group(1))
    # vars: NAME :  (and records of 'NAME : type' inside type decls)
    for m in re.finditer(r'^\s*([A-Za-z]\w*)\s*:', section, flags=re.M):
        names.append(m.group(1))
    # procedure/function headers
    if header_kw:
        for m in re.finditer(r'^\s*(?:' + header_kw + r')\s+([A-Za-z]\w*)',
                             section, flags=re.M):
            names.append(m.group(1))
    return names

def read_text(path):
    raw = open(path, 'rb').read()
    for enc in ('utf-8', 'latin-1'):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            pass
    return raw.decode('latin-1', errors='replace')

def unit_interface_ids(path):
    """Accurately parse a unit's INTERFACE section: top-level CONST names,
    top-level TYPE names (record fields excluded via record-depth tracking),
    top-level VAR names, and PROCEDURE/FUNCTION names."""
    src = strip_comments_strings(read_text(path))
    m = re.search(r'\bINTERFACE\b(.*?)\bIMPLEMENTATION\b', src, flags=re.S)
    if not m:
        return []
    body = m.group(1)
    lines = body.splitlines()
    names = []
    # locate top-level section starts
    def find_line(kw, start=0):
        for i in range(start, len(lines)):
            if re.match(r'^\s*' + kw + r'\b', lines[i], re.I):
                return i
        return None
    const_i = find_line('CONST')
    type_i = find_line('TYPE')
    # first proc/fn header line ends the declaration sections
    proc_i = None
    for i in range(len(lines)):
        if re.match(r'^\s*(?:PROCEDURE|FUNCTION)\b', lines[i], re.I):
            proc_i = i
            break
    def section(a, b):
        a = 0 if a is None else a
        b = len(lines) if b is None else b
        return lines[a + 1:b]  # skip the section keyword line itself
    # CONST: top-level 'name ='
    for ln in section(const_i, type_i if type_i is not None else proc_i):
        mm = re.match(r'^\s*([A-Za-z]\w*)\s*=', ln)
        if mm:
            names.append(mm.group(1))
    # TYPE: top-level 'name =' at record depth 0
    depth = 0
    for ln in section(type_i, proc_i):
        if re.match(r'^\s*RECORD\b', ln, re.I):
            depth += 1
            continue
        if re.match(r'^\s*END\b', ln, re.I):
            depth = max(0, depth - 1)
            continue
        if depth == 0:
            mm = re.match(r'^\s*([A-Za-z]\w*)\s*=', ln)
            if mm:
                names.append(mm.group(1))
    # enumerators of top-level enum types are unit-level constants too
    for mm in re.finditer(r'^\s*[A-Za-z]\w*\s*=\s*\(([^)]*)\)',
                          '\n'.join(section(type_i, proc_i)), flags=re.M | re.S):
        names += re.findall(r'[A-Za-z]\w*', mm.group(1))
    # VAR (if any) + proc/fn headers after the TYPE section.
    # Parenthesis depth tracks multi-line parameter lists so parameter
    # names are never captured.
    if proc_i is not None:
        depth = 0
        paren = 0
        for ln in lines[proc_i:]:
            if paren == 0:
                if re.match(r'^\s*RECORD\b', ln, re.I):
                    depth += 1
                elif re.match(r'^\s*END\b', ln, re.I):
                    depth = max(0, depth - 1)
                elif depth == 0:
                    mm = re.match(r'^\s*(?:PROCEDURE|FUNCTION)\s+([A-Za-z]\w*)', ln, re.I)
                    if mm:
                        names.append(mm.group(1))
                    elif not re.match(r'^\s*(?:VAR|CONST|TYPE)\b', ln, re.I):
                        mm = re.match(r'^\s*([A-Za-z]\w*)\s*:', ln)
                        if mm:
                            names.append(mm.group(1))
            paren += ln.count('(') - ln.count(')')
            paren = max(0, paren)
    return [n for n in names if n.lower() not in PASCAL_KEYWORDS]

def program_scope_ids(path):
    src = strip_comments_strings(read_text(path))
    names = []
    for sect_kw in ('CONST', 'TYPE', 'VAR'):
        sm = re.search(r'^\s*' + sect_kw + r'\b(.*?)(?=^\s*(?:CONST|TYPE|VAR|PROCEDURE|FUNCTION)\b|\Z)',
                       src, flags=re.S | re.M)
        if sm:
            names += decl_names(sm.group(1), None)
    for m in re.finditer(r'^\s*(?:PROCEDURE|FUNCTION)\s+([A-Za-z]\w*)', src, flags=re.M):
        names.append(m.group(1))
    return [n for n in names if n.lower() not in PASCAL_KEYWORDS]

def local_ids(path):
    """All identifiers declared inside procedure/function bodies (info only)."""
    src = strip_comments_strings(read_text(path))
    names = []
    # find each body: from first BEGIN ... to matching END is hard; instead
    # collect VAR sections that are NOT at program level (i.e. indented)
    for m in re.finditer(r'^(\s+)VAR\b(.*?)(?=^\s*(?:BEGIN|END|PROCEDURE|FUNCTION)\b|\Z)',
                         src, flags=re.S | re.M):
        if m.group(1).strip():  # indented => local
            names += decl_names(m.group(2), None)
    return [n for n in names if n.lower() not in PASCAL_KEYWORDS]

def key8(name):
    return name[:8].lower()

def report(ids, title):
    groups = collections.defaultdict(list)
    for n in ids:
        groups[key8(n)].append(n)
    dups = {k: v for k, v in groups.items() if len(v) > 1}
    print(f'--- {title} ---')
    if not dups:
        print('  (no internal collisions)')
    for k, v in sorted(dups.items()):
        print(f'  COLLISION {k.upper()}: {", ".join(v)}')
    return dups

def cross(a, b, title):
    print(f'--- {title} ---')
    found = False
    kb = collections.defaultdict(list)
    for n in b:
        kb[key8(n)].append(n)
    for n in a:
        if key8(n) in kb:
            found = True
            print(f'  COLLISION {key8(n).upper()}: program {n}  vs  unit {", ".join(kb[key8(n)])}')
    if not found:
        print('  (no collisions)')

if __name__ == '__main__':
    prog = sys.argv[1]
    unit = sys.argv[2]
    pids = program_scope_ids(prog)
    uids = unit_interface_ids(unit)
    lids = local_ids(prog)
    print(f'program-scope identifiers: {len(pids)}')
    print(f'syscall interface identifiers: {len(uids)}')
    report(pids, 'program scope vs program scope')
    cross(pids, uids, 'program scope vs syscall unit')
    report(uids, 'syscall unit internal (sanity check)')
    # informational: locals that shadow program scope or unit ids
    print('--- informational: local declarations shadowing program/unit scope ---')
    sup = {key8(n) for n in pids} | {key8(n) for n in uids}
    seen = set()
    shadow = False
    for n in lids:
        k = key8(n)
        if k in sup and k not in seen:
            seen.add(k)
            origin = 'program' if key8(n) in {key8(x) for x in pids} else 'unit'
            print(f'  {k.upper()}: local {n} shadows {origin}')
            shadow = True
    if not shadow:
        print('  (none)')
