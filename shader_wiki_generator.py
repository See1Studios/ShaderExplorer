"""
UE Shader Wiki Generator
Parses .usf / .ush files from UE_5.5 Engine/Shaders and generates an Obsidian vault.

Each shader file → one Markdown note with:
  - Metadata (path, type)
  - Includes section  → [[wikilinks]] to other shader files
  - Structs / Classes
  - Functions
  - Macros (#define)
  - Global variables
"""

import os
import re
import sys
from pathlib import Path
from collections import defaultdict
from urllib.parse import quote

# ── Configuration ──────────────────────────────────────────────────────────────
SHADER_ROOT = Path(r"C:\Program Files\Epic Games\UE_5.5\Engine\Shaders")
VAULT_ROOT  = Path(r"C:\Users\parkj\Desktop\UE_Shaders_Wiki")

# ── Regex patterns ─────────────────────────────────────────────────────────────
RE_INCLUDE   = re.compile(r'#\s*include\s+"([^"]+)"')
RE_STRUCT    = re.compile(r'^\s*struct\s+(\w+)', re.MULTILINE)
RE_CLASS     = re.compile(r'^\s*class\s+(\w+)', re.MULTILINE)
RE_MACRO_DEF = re.compile(r'^#\s*define\s+(\w+)(\([^)]*\))?', re.MULTILINE)

# Function detection strategy:
#   [optional modifiers]  ReturnType  FunctionName ( params ) [const] {
# ReturnType is ANY identifier — not a fixed list.
# Groups: (1) return type, (2) function name, (3) params
RE_FUNCTION = re.compile(
    r'^\s*'
    r'(?:(?:inline|static|FORCEINLINE|FORCENOINLINE|FORCEINLINE_DEBUGGABLE|export)\s+)*'
    r'(\w[\w]*(?:\s*<[^>]{0,60}>)?(?:\s*\[\s*\])?)\s+'   # (1) return type
    r'(\w+)\s*'                                             # (2) function name
    r'\(([^)]{0,500})\)\s*'                                 # (3) params
    r'(?:const\s*)?'
    r'\{',
    re.MULTILINE
)

# Global-variable pattern (type name ; at top level)
RE_GLOBAL_VAR = re.compile(
    r'^(?:static\s+)?(?:const\s+)?'
    r'(?:float[234x]?|half[234x]?|int[234]?|uint[234]?|bool|double|'
    r'Texture2D|SamplerState|RWTexture2D|Buffer|RWBuffer|StructuredBuffer|RWStructuredBuffer)'
    r'(?:<[^>]+>)?\s+'
    r'(\w+)\s*(?:\[[^\]]*\])?\s*;',
    re.MULTILINE
)

# Keywords that must not appear as a return type or function name
HLSL_KEYWORDS = frozenset({
    'if', 'else', 'for', 'while', 'do', 'switch', 'return', 'break', 'continue',
    'case', 'default', 'struct', 'class', 'typedef', 'namespace', 'cbuffer', 'tbuffer',
    'sizeof', 'true', 'false', 'NULL', 'nullptr', 'in', 'out', 'inout',
    'void', 'float', 'int', 'uint', 'bool', 'half', 'double', 'min16float',
    'float1', 'float2', 'float3', 'float4', 'float4x4', 'float3x4', 'float3x3', 'float2x2',
    'int2', 'int3', 'int4', 'uint2', 'uint3', 'uint4',
    'half2', 'half3', 'half4', 'half4x4',
    'Texture2D', 'Texture3D', 'TextureCube', 'SamplerState',
})


# ── Helpers ────────────────────────────────────────────────────────────────────

def resolve_include(include_str: str, current_file: Path) -> Path | None:
    """Try to resolve a #include path to an absolute shader file path.

    UE shader include resolution order:
    1. Virtual paths  /Engine/Public/...  → SHADER_ROOT/Public/...
    2. Virtual paths  /Engine/Private/... → SHADER_ROOT/Private/...
    3. Other /Engine/... virtual paths    → skip (Generated, Plugin shaders, etc.)
    4. Relative to current file directory
    5. Relative to SHADER_ROOT/Private/  (UE default search path)
    6. Relative to SHADER_ROOT/Public/
    7. Relative to SHADER_ROOT/
    """
    # 1–3. UE virtual paths starting with /Engine/
    if include_str.startswith('/Engine/Public/'):
        rel = include_str[len('/Engine/Public/'):]
        p = SHADER_ROOT / 'Public' / rel
        return p if p.exists() else None

    if include_str.startswith('/Engine/Private/'):
        rel = include_str[len('/Engine/Private/'):]
        p = SHADER_ROOT / 'Private' / rel
        return p if p.exists() else None

    if include_str.startswith('/Engine/Shared/'):
        rel = include_str[len('/Engine/Shared/'):]
        p = SHADER_ROOT / 'Shared' / rel
        return p if p.exists() else None

    if include_str.startswith('/'):
        # /Engine/Generated/... or plugin paths — not on disk, skip
        return None

    # 4. Relative to current file
    candidate = (current_file.parent / include_str).resolve()
    if candidate.exists():
        return candidate

    # 5. Relative to Private/ root (UE treats Private/ as a search root)
    p = SHADER_ROOT / 'Private' / include_str
    if p.exists():
        return p

    # 6. Relative to Public/
    p = SHADER_ROOT / 'Public' / include_str
    if p.exists():
        return p

    # 7. Relative to shader root itself
    p = SHADER_ROOT / include_str
    if p.exists():
        return p

    return None


# ── Subsystem tag helpers ──────────────────────────────────────────────────────

def hue_to_rgb(h: float) -> int:
    """Convert hue (0.0–1.0) to an RGB int at full saturation/brightness."""
    import colorsys
    r, g, b = colorsys.hsv_to_rgb(h, 0.65, 0.90)
    return (int(r * 255) << 16) | (int(g * 255) << 8) | int(b * 255)


def build_subsystem_colors(shader_root: Path) -> dict[str, int]:
    """Auto-discover all first-level subdirectories and assign evenly-spaced hues."""
    folders = sorted(
        d.name for d in shader_root.iterdir()
        if d.is_dir() and d.name not in ('Private', 'Public', 'Shared')
    )
    # Collect subdirs of Private/ as the main subsystems
    private = shader_root / "Private"
    subsystems = sorted(d.name for d in private.iterdir() if d.is_dir()) if private.exists() else []

    color_map: dict[str, int] = {}
    n = len(subsystems)
    for i, name in enumerate(subsystems):
        color_map[name] = hue_to_rgb(i / n)
    # Fixed colors for top-level groups
    color_map["Public"] = 0x26C6DA
    color_map["Shared"] = 0xA5D6A7
    return color_map


def folder_to_tag(folder: str, color_map: dict[str, int]) -> str:
    """Return the most specific subsystem tag for a folder path."""
    parts = folder.replace("\\", "/").split("/")
    # Walk from most-specific to least-specific part
    for part in reversed(parts):
        if part in color_map:
            return f"#{part}"
    return ""


def file_to_note_name(path: Path) -> str:
    """Return path relative to SHADER_ROOT, forward-slash separated."""
    try:
        rel = path.relative_to(SHADER_ROOT)
    except ValueError:
        rel = Path(path.name)
    return str(rel).replace("\\", "/")


def note_slug(path: Path) -> str:
    """Wikilink target = just the filename (no folder prefix).
    Unique across the whole vault since there are no duplicate filenames."""
    return path.name


def note_save_path(path: Path, notes_dir: Path) -> Path:
    """Destination .md path, preserving subfolder structure under notes_dir."""
    try:
        rel_folder = path.parent.relative_to(SHADER_ROOT)
    except ValueError:
        rel_folder = Path("")
    dest_dir = notes_dir / rel_folder
    dest_dir.mkdir(parents=True, exist_ok=True)
    return dest_dir / (path.name + ".md")


def extract_struct_members(text: str, struct_name: str) -> list[str]:
    """Extract member declarations from a struct body using brace matching."""
    # Find 'struct Name' followed by optional content then '{'
    pattern = re.compile(
        r'\bstruct\s+' + re.escape(struct_name) + r'\b[^;{]*\{',
        re.MULTILINE
    )
    m = pattern.search(text)
    if not m:
        return []

    # Walk forward counting braces to find the matching '}'
    start = m.end()
    depth = 1
    i = start
    while i < len(text) and depth > 0:
        if text[i] == '{':
            depth += 1
        elif text[i] == '}':
            depth -= 1
        i += 1
    body = text[start:i - 1]

    # Parse member lines: skip preprocessor, nested struct/function bodies
    # Member pattern:  type  name [array] [: SEMANTIC] ;
    RE_MEMBER = re.compile(
        r'^\s*'
        r'(?:(?:static|const|nointerpolation|linear|centroid|sample|noperspective|'
        r'row_major|column_major|precise|globallycoherent)\s+)*'
        r'(\w+(?:\s*<[^>]{0,40}>)?)\s+'        # type
        r'(\w+)\s*(?:\[[^\]]*\])?\s*'           # name + optional array
        r'(?::\s*\w+\s*)?;',                    # optional : SEMANTIC ;
        re.MULTILINE
    )

    members = []
    depth = 0
    for line in body.splitlines():
        depth += line.count('{') - line.count('}')
        if depth > 0:   # inside nested struct/union/block — skip
            continue
        mm = RE_MEMBER.match(line)
        if mm:
            mem_type, mem_name = mm.group(1), mm.group(2)
            if mem_name not in HLSL_KEYWORDS:
                members.append(f"{mem_type} {mem_name}")
    return members


def parse_file(path: Path) -> dict:
    """Parse a single shader file and return extracted symbols."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return {}

    # Strip block comments to avoid false matches
    text_no_block = re.sub(r'/\*.*?\*/', '', text, flags=re.DOTALL)
    # Strip line comments (keep original text for includes)
    text_clean = re.sub(r'//[^\n]*', '', text_no_block)

    def normalize(s: str) -> str:
        return ' '.join(s.split())

    def dedup(lst):
        seen = set()
        return [x for x in lst if not (x in seen or seen.add(x))]

    includes = RE_INCLUDE.findall(text)
    struct_names = dedup([m for m in RE_STRUCT.findall(text_clean) if m not in HLSL_KEYWORDS])
    classes      = dedup([m for m in RE_CLASS.findall(text_clean)  if m not in HLSL_KEYWORDS])
    macros       = dedup([m[0] + (m[1] if m[1] else '') for m in RE_MACRO_DEF.findall(text_clean)])

    # Structs with members
    structs = []
    for name in struct_names:
        members = extract_struct_members(text_clean, name)
        structs.append({"name": name, "members": dedup(members)})

    functions = []
    for ret_type, func_name, params in RE_FUNCTION.findall(text_clean):
        if func_name in HLSL_KEYWORDS:
            continue
        sig = f"{normalize(ret_type)} {func_name}({normalize(params)})"
        functions.append(sig)
    functions = dedup(functions)

    global_vars = dedup(RE_GLOBAL_VAR.findall(text_clean))

    return {
        "includes":    dedup(includes),
        "structs":     structs,
        "classes":     classes,
        "macros":      macros,
        "functions":   functions,
        "global_vars": global_vars,
    }


def virtual_slug(include_str: str) -> str:
    """Wikilink target for an unresolvable virtual path — just the filename.
    Dummy notes are saved as Notes/_Virtual/<filename>.md
    """
    return include_str.split('/')[-1]


def build_include_map(all_files: list[Path]) -> dict[Path, list[Path]]:
    """Build file → resolved include paths map."""
    inc_map = {}
    for f in all_files:
        data = parse_file(f)
        resolved = []
        for inc in data.get("includes", []):
            r = resolve_include(inc, f)
            if r and r != f:
                resolved.append(r)
        inc_map[f] = resolved
    return inc_map


def build_claude_url(rel_path: str, data: dict, max_chars: int = 2000) -> str:
    """Build a claude.ai/new URL pre-filled with a shader analysis prompt."""
    includes  = data.get("includes", [])
    structs   = data.get("structs", [])
    functions = data.get("functions", [])
    macros    = data.get("macros", [])

    parts = [
        f"다음 Unreal Engine 5.5 셰이더 파일을 분석하고, 목적과 핵심 개념, "
        f"렌더링 파이프라인에서의 역할을 한국어로 설명해줘.\n",
        f"File: {rel_path}\n",
    ]
    if includes:
        parts.append("Includes: " + ", ".join(includes[:8])
                     + (f" (+{len(includes)-8} more)" if len(includes) > 8 else "") + "\n")
    if structs:
        struct_lines = []
        for s in structs[:5]:
            members = s["members"][:6]
            mem_str = ", ".join(members) + ("..." if len(s["members"]) > 6 else "")
            struct_lines.append(f"  - {s['name']}: {mem_str}" if mem_str else f"  - {s['name']}")
        parts.append("Structs:\n" + "\n".join(struct_lines) + "\n")
    if functions:
        shown = functions[:15]
        parts.append("Functions (" + str(len(functions)) + " total):\n"
                     + "\n".join(f"  - {f}" for f in shown)
                     + (f"\n  ... ({len(functions)-15} more)" if len(functions) > 15 else "") + "\n")
    if macros:
        parts.append("Macros: " + ", ".join(f"#{m}" for m in macros[:8])
                     + (f" (+{len(macros)-8} more)" if len(macros) > 8 else "") + "\n")

    prompt = "".join(parts)
    if len(prompt) > max_chars:
        prompt = prompt[:max_chars] + "\n...(truncated)"

    return f"https://claude.ai/new?q={quote(prompt)}"


def generate_note(path: Path, data: dict, all_file_slugs: dict[Path, str], color_map: dict[str, int] = {}) -> str:
    """Generate Obsidian markdown content for one shader file."""
    rel_path   = file_to_note_name(path)
    slug       = note_slug(path)
    ext        = path.suffix.lower()
    if ext == ".ush":
        file_type = "Header (.ush)"
    elif ext == ".h":
        file_type = "Shared Header (.h)"
    else:
        file_type = "Shader (.usf)"
    folder     = str(path.parent.relative_to(SHADER_ROOT)).replace("\\", "/") if path.is_relative_to(SHADER_ROOT) else ""

    uri_path = str(path).replace("\\", "/").replace(" ", "%20")
    subsystem_tag = folder_to_tag(folder, color_map)

    # YAML frontmatter — shown in Obsidian Properties panel
    lines = ["---"]
    lines.append(f"type: {file_type}")
    lines.append(f"path: {rel_path}")
    if subsystem_tag:
        lines.append(f"tags: [{subsystem_tag.lstrip('#')}]")
    lines.append("---")
    lines.append("")
    lines.append(f"[VS Code](vscode://file/{uri_path})")
    lines.append("")

    # ── Includes ──
    includes = data.get("includes", [])
    if includes:
        lines.append(f"> [!includes]+ Includes ({len(includes)})")
        for inc in includes:
            resolved = resolve_include(inc, path)
            if resolved and resolved in all_file_slugs:
                target_slug = all_file_slugs[resolved]
                lines.append(f"> - [[{target_slug}]] `{inc}`")
            elif inc.startswith('/'):
                lines.append(f"> - [[{virtual_slug(inc)}]] `{inc}`")
            else:
                lines.append(f"> - `{inc}`")
        lines.append("")

    # ── Structs / Classes ──
    structs = data.get("structs", [])
    classes = data.get("classes", [])
    if structs or classes:
        count = len(structs) + len(classes)
        lines.append(f"> [!types]+ Types ({count})")
        lines.append("> ```hlsl")
        for s in structs:
            lines.append(f"> struct {s['name']} {{")
            for mem in s["members"]:
                lines.append(f">     {mem};")
            lines.append("> }")
        for c in classes:
            lines.append(f"> class {c} {{}}")
        lines.append("> ```")
        lines.append("")

    # ── Functions ──
    functions = data.get("functions", [])
    if functions:
        lines.append(f"> [!functions]+ Functions ({len(functions)})")
        lines.append("> ```hlsl")
        for fn in functions[:200]:
            lines.append(f"> {fn}")
        if len(functions) > 200:
            lines.append(f"> // ... {len(functions) - 200} more")
        lines.append("> ```")
        lines.append("")

    # ── Macros ──
    macros = data.get("macros", [])
    if macros:
        lines.append(f"> [!macros]+ Macros ({len(macros)})")
        lines.append("> ```hlsl")
        for m in macros[:100]:
            lines.append(f"> #define {m}")
        if len(macros) > 100:
            lines.append(f"> // ... {len(macros) - 100} more")
        lines.append("> ```")
        lines.append("")

    # ── Global Variables ──
    gvars = data.get("global_vars", [])
    if gvars:
        lines.append(f"> [!globals]+ Global Variables ({len(gvars)})")
        lines.append("> ```hlsl")
        for v in gvars[:50]:
            lines.append(f"> {v}")
        if len(gvars) > 50:
            lines.append(f"> // ... {len(gvars) - 50} more")
        lines.append("> ```")
        lines.append("")

    return "\n".join(lines)


def generate_index(all_files: list[Path], all_slugs: dict[Path, str]) -> str:
    """Generate an index note listing all shader files grouped by folder."""
    by_folder = defaultdict(list)
    for f in sorted(all_files):
        try:
            folder = str(f.parent.relative_to(SHADER_ROOT)).replace("\\", "/")
        except ValueError:
            folder = "Other"
        by_folder[folder].append(f)

    lines = ["# UE 5.5 Shader Index", "", f"Total files: **{len(all_files)}**", ""]
    for folder in sorted(by_folder):
        lines.append(f"## {folder or 'Root'}")
        for f in sorted(by_folder[folder]):
            lines.append(f"- `{f.name}`")
        lines.append("")
    return "\n".join(lines)


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    print(f"Scanning shaders in: {SHADER_ROOT}")
    all_files = sorted(
        list(SHADER_ROOT.rglob("*.usf")) +
        list(SHADER_ROOT.rglob("*.ush")) +
        list(SHADER_ROOT.rglob("*.h"))
    )
    print(f"Found {len(all_files)} shader files")

    # Build slug map first (needed for wikilinks)
    all_slugs: dict[Path, str] = {f: note_slug(f) for f in all_files}
    color_map = build_subsystem_colors(SHADER_ROOT)
    print(f"Subsystems found: {len(color_map) - 2}")

    # Create vault directory structure
    VAULT_ROOT.mkdir(parents=True, exist_ok=True)
    notes_dir = VAULT_ROOT / "Notes"
    notes_dir.mkdir(exist_ok=True)

    # Write .obsidian config for graph view
    obsidian_dir = VAULT_ROOT / ".obsidian"
    obsidian_dir.mkdir(exist_ok=True)
    app_json = obsidian_dir / "app.json"
    app_json.write_text('{}', encoding='utf-8')

    # Build color groups dynamically from color_map
    color_entries = []
    for tag, rgb in sorted(color_map.items()):
        color_entries.append(f'    {{ "query": "tag:#{tag}", "color": {{ "a": 1, "rgb": {rgb} }} }}')
    color_entries.append(f'    {{ "query": "tag:#generated",    "color": {{ "a": 1, "rgb": {0x757575} }} }}')
    color_entries.append(f'    {{ "query": "tag:#platform-sdk", "color": {{ "a": 1, "rgb": {0xEF9A9A} }} }}')
    color_groups_json = ",\n".join(color_entries)

    graph_json = obsidian_dir / "graph.json"
    graph_json.write_text(f"""{{
  "collapse-filter": false,
  "search": "",
  "showTags": false,
  "showAttachments": false,
  "hideUnresolved": false,
  "showOrphans": true,
  "collapse-color-groups": false,
  "colorGroups": [
{color_groups_json}
  ],
  "collapse-display": false,
  "showArrow": true,
  "textFadeMultiplier": 0,
  "nodeSizeMultiplier": 1,
  "lineSizeMultiplier": 1,
  "collapse-forces": false,
  "centerStrength": 0.518713,
  "repelStrength": 10,
  "linkStrength": 1,
  "linkDistance": 30,
  "scale": 1,
  "close": false
}}""", encoding='utf-8')

    # Parse and write notes — collect virtual includes as we go
    virtual_includes: dict[str, str] = {}  # slug → original path
    total = len(all_files)
    for i, f in enumerate(all_files, 1):
        if i % 100 == 0 or i == total:
            print(f"  [{i}/{total}] {f.name}")

        data = parse_file(f)
        content = generate_note(f, data, all_slugs, color_map)

        # Track virtual includes (unresolvable paths starting with /)
        for inc in data.get("includes", []):
            if inc.startswith('/') and resolve_include(inc, f) is None:
                slug = virtual_slug(inc)
                virtual_includes[slug] = inc

        # Subfolder structure matching original shader directory layout
        note_path = note_save_path(f, notes_dir)
        note_path.write_text(content, encoding='utf-8')

    # Write dummy notes for virtual / generated includes
    dummy_dir = VAULT_ROOT / "Notes" / "_Virtual"
    dummy_dir.mkdir(parents=True, exist_ok=True)
    print(f"Writing {len(virtual_includes)} dummy notes for virtual includes...")
    for slug, orig_path in sorted(virtual_includes.items()):
        filename = orig_path.split('/')[-1]
        if orig_path.startswith('/Engine/Generated/'):
            category = "Generated"
            description = (
                "이 파일은 UE 셰이더 컴파일러가 **런타임에 자동 생성**하는 가상 헤더입니다.\n"
                "디스크에 실제 파일로 존재하지 않으며, 엔진이 셰이더를 컴파일할 때 메모리 상에서 생성됩니다."
            )
            if 'UniformBuffer' in orig_path or orig_path.endswith('GeneratedUniformBuffers.ush'):
                description += "\n\n**Uniform Buffer** 구조체를 자동 생성하여 셰이더에 바인딩합니다."
            elif 'Material.ush' in orig_path:
                description += "\n\n**Material** 파라미터 및 표현식을 인라인 HLSL로 생성합니다."
            elif 'VertexFactory.ush' in orig_path:
                description += "\n\n**Vertex Factory** 인터페이스를 구현체에 맞게 생성합니다."
        elif orig_path.startswith('/Platform/'):
            category = "Platform SDK"
            description = (
                "이 파일은 **콘솔/플랫폼 SDK** (PlayStation, Xbox 등) 전용 셰이더 헤더입니다.\n"
                "해당 플랫폼 라이선스가 없는 표준 PC 설치에는 포함되지 않습니다."
            )
        else:
            category = "Virtual"
            description = "가상 경로로 참조되는 셰이더 헤더입니다."

        content = "\n".join([
            f"# {filename}",
            "",
            "## Metadata",
            f"- **Type**: {category} (dummy note)",
            f"- **Virtual Path**: `{orig_path}`",
            f"- **Tags**: #{category.lower().replace(' ', '-')}",
            "",
            "## Description",
            description,
            "",
            "> [!note] 더미 노트",
            "> 실제 파일이 아닙니다. include 그래프 시각화를 위해 자동 생성된 플레이스홀더입니다.",
        ])
        (dummy_dir / (filename + ".md")).write_text(content, encoding='utf-8')

    # Write index
    index_content = generate_index(all_files, all_slugs)
    (VAULT_ROOT / "00 Index.md").write_text(index_content, encoding='utf-8')

    # Write README
    readme = f"""# UE 5.5 Shader Wiki

Obsidian vault generated from `{SHADER_ROOT}`.

## How to open
1. Open Obsidian
2. **Open folder as vault** → select `{VAULT_ROOT}`
3. Trust the vault
4. Open **Graph view** (Ctrl+G) to explore include dependencies

## Structure
- `00 Index.md` — full list of all {total} shader files
- `Notes/Private/` — Private shader headers and sources
- `Notes/Public/` — Public shader API headers

## Graph color coding
- **Blue nodes** = `.ush` header files
- **Orange nodes** = `.usf` shader entry points

## Tips
- Click any node in graph view to open the note
- Use **local graph** (right-click a note) to see only its direct dependencies
- Search for a struct or function name in Obsidian search to find which files define/use it
"""
    (VAULT_ROOT / "README.md").write_text(readme, encoding='utf-8')

    print(f"\nDone! Vault created at: {VAULT_ROOT}")
    print(f"Open Obsidian → 'Open folder as vault' → {VAULT_ROOT}")


if __name__ == "__main__":
    main()
