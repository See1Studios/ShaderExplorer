#!/usr/bin/env python3
"""
UE5 Shader Explorer Generator

Usage:
    python shader_explorer.py "C:/Program Files/Epic Games/UE_5.5/Engine/Shaders"
    python shader_explorer.py ./MyProject/Shaders

Generates: ShaderExplorer_generated.html (open in browser, no server needed)
"""

import sys, os, re, json, html

EXTENSIONS = {'.ush', '.usf', '.hlsl', '.h'}

def parse_file(filename, content):
    lines = content.split('\n')
    includes, structs, functions, defines = [], [], [], []
    inc_re = re.compile(r'#include\s+"([^"]+)"')
    struct_re = re.compile(r'^\s*struct\s+(\w+)')
    def_re = re.compile(r'^\s*#define\s+(\w+)(?:\s*\([^)]*\))?\s*(.*?)\\?$')
    func_re = re.compile(r'^[ \t]*(?:inline\s+|static\s+|export\s+)*([\w]+)\s+(\w+)\s*\(')
    skip = {'if','else','for','while','switch','return','struct','class','enum','do','case',
            'defined','pragma','ifdef','ifndef','endif','elif','include','define','undef','error','warning'}

    in_struct = False
    s_name = s_line = depth = 0
    s_members = []

    for i, line in enumerate(lines):
        ls = line.strip()
        m = inc_re.search(ls)
        if m:
            parts = m.group(1).split('/')
            includes.append(parts[-1])
            continue

        if not in_struct:
            m = struct_re.match(ls)
            if m:
                in_struct = True
                s_name = m.group(1)
                s_line = i + 1
                s_members = []
                depth = ls.count('{') - ls.count('}')
                continue

        if in_struct:
            depth += ls.count('{') - ls.count('}')
            if depth <= 0:
                structs.append({'n': s_name, 'm': s_members[:6], 'l': s_line})
                in_struct = False
            elif ls and not ls.startswith('//') and not ls.startswith('#') and not ls.startswith('{'):
                pm = re.match(r'^([\w]+)\s+(\w+)', ls)
                if pm:
                    s_members.append(f"{pm.group(1)} {pm.group(2)}")
            continue

        m = def_re.match(ls)
        if m:
            dn = m.group(1)
            if not dn.startswith('_') and dn != 'PRAGMA_ONCE' and len(dn) > 2:
                defines.append({'n': dn, 'v': (m.group(2) or '').strip()[:30], 'l': i + 1})
            continue

        m = func_re.match(line)
        if m:
            rtype, fname = m.group(1), m.group(2)
            if fname not in skip and rtype not in skip and not fname.startswith('_'):
                functions.append({'n': fname, 'r': rtype, 'l': i + 1})

    return {
        'f': filename,
        'i': includes,
        's': structs,
        'fn': functions,
        'd': defines[:40],
    }


def scan_folder(root_path):
    results = []
    root_path = os.path.normpath(root_path)
    for dirpath, _, filenames in os.walk(root_path):
        for fname in sorted(filenames):
            ext = os.path.splitext(fname)[1].lower()
            if ext not in EXTENSIONS:
                continue
            filepath = os.path.join(dirpath, fname)
            reldir = os.path.relpath(dirpath, root_path).replace('\\', '/')
            if reldir == '.':
                reldir = ''
            try:
                with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
                    content = f.read()
                parsed = parse_file(fname, content)
                parsed['dir'] = reldir
                parsed['src'] = content
                results.append(parsed)
            except Exception as e:
                print(f"  Skip {filepath}: {e}", file=sys.stderr)
    return results


def generate_html(data):
    """Read the template HTML and inject embedded data, removing the file picker."""
    template_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'ShaderExplorer.html')

    if os.path.exists(template_path):
        with open(template_path, 'r', encoding='utf-8') as f:
            template = f.read()
    else:
        print(f"Warning: {template_path} not found, generating standalone.", file=sys.stderr)
        template = None

    # Serialize data as JSON, escape for embedding in <script>
    json_data = json.dumps(data, ensure_ascii=False, separators=(',', ':'))
    # Escape </script> sequences
    json_data = json_data.replace('</script>', '<\\/script>')

    if template:
        # Inject data and auto-init: add a script before the babel script that sets window.__SHADER_DATA__
        data_script = f'<script>window.__SHADER_DATA__={json_data};</script>'
        # Insert before the babel script tag
        template = template.replace('<script type="text/babel">', data_script + '\n<script type="text/babel">')

        # Modify the App init: replace the file picker landing with auto-load
        # Add auto-load useEffect after inputRef
        auto_load = """
  // Auto-load embedded data
  useEffect(()=>{
    if(window.__SHADER_DATA__&&!data){
      const d=window.__SHADER_DATA__;
      setData(d);
      const c=d.find(r=>r.f==='Common.ush');
      setSelected(c?'Common.ush':d[0]?.f||'');
    }
  },[]);
"""
        template = template.replace(
            "const inputRef=useRef(null);",
            "const inputRef=useRef(null);" + auto_load
        )

        stats_info = f"{len(data)} files"
        title = f'<title>UE5 Shader Explorer ({stats_info})</title>'
        template = re.sub(r'<title>.*?</title>', title, template)

        return template

    # Fallback: return a minimal message
    return f"<html><body><pre>Generated {len(data)} files. Template not found.</pre></body></html>"


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    folder = sys.argv[1]
    if not os.path.isdir(folder):
        print(f"Error: '{folder}' is not a directory.", file=sys.stderr)
        sys.exit(1)

    output = sys.argv[2] if len(sys.argv) > 2 else 'ShaderExplorer_generated.html'

    print(f"Scanning: {folder}")
    data = scan_folder(folder)
    print(f"Parsed: {len(data)} files")

    if not data:
        print("No shader files found.", file=sys.stderr)
        sys.exit(1)

    total_s = sum(len(f['s']) for f in data)
    total_fn = sum(len(f['fn']) for f in data)
    total_d = sum(len(f['d']) for f in data)
    print(f"Symbols: {total_s} structs, {total_fn} functions, {total_d} defines")

    html_content = generate_html(data)
    with open(output, 'w', encoding='utf-8') as f:
        f.write(html_content)

    size_mb = os.path.getsize(output) / (1024 * 1024)
    print(f"Output: {output} ({size_mb:.1f} MB)")
    print("Open in browser to use.")


if __name__ == '__main__':
    main()
