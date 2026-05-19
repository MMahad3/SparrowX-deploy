import json
import sys
import re
from pathlib import Path
from ruamel.yaml import YAML


# --------------- service section resolution (case-insensitive) ---------------

def service_to_section(service):
    parts = service.split("-")
    camel = "".join(p.capitalize() for p in parts)
    return camel + "Service"

def service_to_camel(service):
    parts = service.split("-")
    return parts[0] + "".join(p.capitalize() for p in parts[1:])

def normalize_key(s):
    """Lowercase and remove non-alphanumeric for case-insensitive matching."""
    return re.sub(r'[^a-z0-9]', '', s.lower())

def resolve_service_section_key(data, service):
    """
    Case-insensitively find the service section key in the data dict.
    Returns the original key name (e.g., 'ChannelService') or None.
    """
    if not isinstance(data, dict):
        return None
    target_norm = normalize_key(service)
    # Possible variations: with/without "Service", camelCase, etc.
    candidates = [
        service,
        service_to_section(service),
        service_to_camel(service),
        service_to_camel(service) + "Service",
        service.replace("-", ""),
        service.replace("-", "") + "service",
    ]
    # Exact match first
    for candidate in candidates:
        if candidate in data:
            return candidate
    # Case-insensitive scan through all keys
    for key in data.keys():
        if normalize_key(key) == target_norm:
            return key
        if normalize_key(key) == normalize_key(service + "Service"):
            return key
    return None


# --------------- path helpers ---------------

def parse_path_parts(path):
    parts = []
    for p in path.split("."):
        if p:
            parts.append(int(p) if p.isdigit() else p)
    return parts

def get_by_path(root, path_parts):
    cur = root
    for p in path_parts:
        try:
            cur = cur[p]
        except (KeyError, IndexError, TypeError):
            return False, None
    return True, cur

def iter_scalar_paths(node, prefix):
    if isinstance(node, dict):
        for k, v in node.items():
            yield from iter_scalar_paths(v, prefix + [k])
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from iter_scalar_paths(v, prefix + [i])
    else:
        yield prefix, node

def matches_tail(path_parts, tail):
    return bool(tail) and len(path_parts) >= len(tail) and path_parts[-len(tail):] == tail


# --------------- line-level surgical replacement ---------------

def get_value_line(data, path_parts):
    """Return the (0-based) line number where the leaf VALUE lives using ruamel lc."""
    cur = data
    for p in path_parts[:-1]:
        try:
            cur = cur[p]
        except (KeyError, IndexError, TypeError):
            return None
    leaf = path_parts[-1]
    try:
        if isinstance(cur, list):
            row, _ = cur.lc.item(leaf)
        else:
            row, _ = cur.lc.value(leaf)
        return row
    except Exception:
        return None

def get_node_line(data, path_parts):
    """Return the line number (0-based) where the node at path_parts starts (the key line)."""
    cur = data
    for p in path_parts[:-1]:
        try:
            cur = cur[p]
        except (KeyError, IndexError, TypeError):
            return None
    leaf = path_parts[-1]
    try:
        if isinstance(cur, dict):
            row, _ = cur.lc.value(leaf)
            return row
        elif isinstance(cur, list):
            row, _ = cur.lc.item(leaf)
            return row
        else:
            return get_value_line(data, path_parts)
    except Exception:
        return None

def scalar_to_str(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    s = str(value)
    needs_quote = (
        not s
        or s[0] in ('"', "'", '{', '[', '|', '>', '&', '*', '!', '%', '@', '`')
        or ':' in s or '#' in s
        or s.lower() in ('true', 'false', 'null', 'yes', 'no', 'on', 'off')
        or s != s.strip()
    )
    if needs_quote:
        escaped = s.replace('\\', '\\\\').replace('"', '\\"')
        return f'"{escaped}"'
    return s

def patch_line(original_line, leaf_key, new_value):
    leaf_str = str(leaf_key)
    pattern = re.compile(
        r'^(\s*' + re.escape(leaf_str) + r'\s*:\s*)([^#\r\n]*)(.*)',
        re.DOTALL
    )
    line_body = original_line.rstrip('\r\n')
    m = pattern.match(line_body)
    if not m:
        return None
    prefix = m.group(1)
    suffix = m.group(3)
    ending = '\r\n' if original_line.endswith('\r\n') else '\n'
    new_val_str = scalar_to_str(new_value)
    comment_part = (" " + suffix.strip()) if suffix.strip() else ""
    return prefix + new_val_str + comment_part + ending


# --------------- HELPER FOR INDENTATION DETECTION ---------------

def get_child_indentation(lines, parent_line_no, parent_indent):
    """
    Scan lines after parent_line_no until indentation <= parent_indent.
    Find the first non‑empty, non‑comment line that contains a colon (':')
    and return its leading spaces. If no such child exists, return parent_indent + 2.
    """
    child_indent = parent_indent + 2  # default
    i = parent_line_no + 1
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        # Stop when indentation is less or equal to parent (end of block)
        line_indent = len(line) - len(line.lstrip())
        if line_indent <= parent_indent:
            break
        # Look for a key (contains ':') that is not a comment line
        if ':' in line and not line.lstrip().startswith('#'):
            child_indent = line_indent
            break
        i += 1
    return child_indent


# --------------- ADVANCED NESTED INSERTION ---------------

def insert_nested_key(lines, data, service_section_key, key_parts, value, separator='.'):
    """
    Insert a key at the correct nesting level.
    If the parent path exists, insert inside the deepest existing parent.
    Else, fall back to flat insertion at service level.
    """
    # Find service section start line
    section_re = re.compile(rf'^{re.escape(service_section_key)}:')
    start_idx = None
    for i, line in enumerate(lines):
        if section_re.match(line.lstrip()):
            start_idx = i
            break
    if start_idx is None:
        print(f"Cannot find service section '{service_section_key}'")
        return False

    base_indent = len(lines[start_idx]) - len(lines[start_idx].lstrip())

    # Special case: no nesting (single-level key) -> insert as flat key at service level
    if len(key_parts) == 1:
        return insert_flat_key_at_level(lines, start_idx, base_indent, key_parts[0], value)

    # Find the deepest existing parent in the target data structure
    full_path_base = [service_section_key]
    deepest_parent_parts = []
    parent_line_no = None

    # Check from longest parent downwards
    for i in range(len(key_parts)-1, 0, -1):
        candidate_parts = full_path_base + key_parts[:i]
        line_no = get_node_line(data, candidate_parts)
        if line_no is not None:
            deepest_parent_parts = key_parts[:i]
            parent_line_no = line_no
            break

    if not deepest_parent_parts:
        # No parent found (even the first level doesn't exist) -> fall back to flat key at service level
        print(f"No existing parent for '{'.'.join(key_parts)}'. Inserting as flat key.")
        return insert_flat_key_at_level(lines, start_idx, base_indent, separator.join(key_parts), value)

    # Now we have the line number where the deepest parent starts
    parent_line = lines[parent_line_no]
    parent_indent = len(parent_line) - len(parent_line.lstrip())
    # Use detected indentation of existing children (if any)
    child_indent = get_child_indentation(lines, parent_line_no, parent_indent)

    # Find the end of the parent block (next line with same or less indentation)
    end_idx = parent_line_no + 1
    # Also find the end of the service section to avoid going beyond
    end_section_idx = len(lines)
    for i in range(start_idx + 1, len(lines)):
        if lines[i].strip() and not lines[i].startswith(' ' * (base_indent + 2)):
            end_section_idx = i
            break
    while end_idx < end_section_idx:
        line = lines[end_idx]
        if line.strip() and len(line) - len(line.lstrip()) <= parent_indent:
            break
        end_idx += 1

    # Insert the new leaf key inside this block
    leaf_part = key_parts[-1]
    new_line = f"{' ' * child_indent}{leaf_part}: {scalar_to_str(value)}\n"
    lines.insert(end_idx, new_line)
    print(f"Inserted '{leaf_part}' under '{'.'.join(deepest_parent_parts)}' in section '{service_section_key}'")
    return True

def insert_flat_key_at_level(lines, start_idx, base_indent, flat_key, value):
    """Insert a flat key at the end of the service section, using existing child indentation."""
    # Find the end of the service section block (first line with indentation <= base_indent)
    end_idx = start_idx + 1
    while end_idx < len(lines):
        line = lines[end_idx]
        if line.strip() and not line.startswith(' ' * (base_indent + 2)):
            break
        end_idx += 1

    # Detect existing child indentation under this service section
    child_indent = base_indent + 2  # default
    for i in range(start_idx + 1, end_idx):
        line = lines[i]
        if not line.strip():
            continue
        line_indent = len(line) - len(line.lstrip())
        if line_indent > base_indent:
            child_indent = line_indent
            break

    flat_key_line = f"{' ' * child_indent}{flat_key}: {scalar_to_str(value)}\n"
    lines.insert(end_idx, flat_key_line)
    print(f"Inserted flat key '{flat_key}' at end of section")
    return True


# --------------- MAIN ---------------

def main():
    file_path = Path("chart/values.yaml")
    changes_path = Path("ai_output.json")
    source_changes_path = Path("changes.json")

    if not file_path.exists():
        print("Target file not found: chart/values.yaml")
        sys.exit(1)

    with changes_path.open("r", encoding="utf-8") as f:
        rows = json.load(f)

    source_changes_lookup = {}
    if source_changes_path.exists():
        with source_changes_path.open("r", encoding="utf-8") as f:
            for r in json.load(f):
                source_changes_lookup[(r.get("service"), r.get("key"))] = r

    original_text = file_path.read_text(encoding="utf-8")
    yaml_parser = YAML(typ="rt")
    yaml_parser.preserve_quotes = True
    data = yaml_parser.load(original_text) or {}

    lines = original_text.splitlines(keepends=True)
    if lines and not lines[-1].endswith('\n'):
        lines[-1] += '\n'

    applied = 0
    skipped = 0

    for row in rows:
        service = row.get("service")
        key = row.get("key")
        value = row.get("value")

        if not service or not key:
            print(f"Skipping invalid row: {row}")
            skipped += 1
            continue

        service_section_key = resolve_service_section_key(data, service)
        if not service_section_key:
            print(f"Skipping: no service section found for '{service}' (case-insensitive)")
            skipped += 1
            continue

        service_root = data[service_section_key]

        # Strip any service-prefix from the key.
        rel_key = key
        for prefix in (f"{service_section_key}.", f"{service_to_section(service)}.",
                    f"{service_to_camel(service)}."):
            if rel_key.startswith(prefix):
                rel_key = rel_key[len(prefix):]
                break

        rel_parts = parse_path_parts(rel_key)
        if not rel_parts:
            print(f"Skipping empty path for row: {row}")
            skipped += 1
            continue

        source_row = source_changes_lookup.get((service, key), {})
        old_value = source_row.get("old_value")

        direct_exists, _ = get_by_path(service_root, rel_parts)
        chosen_parts = rel_parts if direct_exists else None

        if chosen_parts is None:
            candidates = [
                (p, v) for p, v in iter_scalar_paths(service_root, [])
                if matches_tail(p, rel_parts) or p[-1:] == rel_parts[-1:]
            ]
            if old_value is not None:
                narrow = [c for c in candidates if c[1] == old_value]
                if narrow:
                    candidates = narrow
            if len(candidates) == 1:
                chosen_parts = candidates[0][0]
            elif len(candidates) > 1:
                paths_str = ", ".join(".".join(str(x) for x in c[0]) for c in candidates)
                print(f"Skipping ambiguous match for {service_section_key}.{rel_key}: [{paths_str}]")
                skipped += 1
                continue

        if chosen_parts is None:
            # Try to insert as a new nested key (handles deep parents)
            inserted = insert_nested_key(lines, data, service_section_key, rel_parts, value, separator='.')
            if inserted:
                applied += 1
                continue
            else:
                print(f"Skipping: path not found and insertion failed for {service_section_key}.{'.'.join(rel_parts)}")
                skipped += 1
                continue

        full_parts = [service_section_key] + chosen_parts
        line_no = get_value_line(data, full_parts)

        if line_no is None or line_no >= len(lines):
            print(f"Skipping: could not locate line for {'.'.join(str(p) for p in full_parts)}")
            skipped += 1
            continue

        new_line = patch_line(lines[line_no], full_parts[-1], value)
        if new_line is None:
            print(f"Skipping: line regex did not match for {'.'.join(str(p) for p in full_parts)}")
            skipped += 1
            continue

        dotted = ".".join(str(p) for p in full_parts)
        print(f"Patching line {line_no + 1}: {dotted} = {json.dumps(value)}")
        lines[line_no] = new_line
        applied += 1

    if applied > 0:
        file_path.write_text("".join(lines), encoding="utf-8")
    else:
        print("No updates applied; chart/values.yaml left untouched.")

    print(f"Applied {applied} change(s), skipped {skipped} change(s).")


if __name__ == "__main__":
    main()
