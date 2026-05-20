import argparse
import json
import re
import sys
from pathlib import Path

from ruamel.yaml import YAML


def service_to_section(service):
    parts = service.split("-")
    camel = "".join(p.capitalize() for p in parts)
    return camel + "Service"


def service_to_camel(service):
    parts = service.split("-")
    return parts[0] + "".join(p.capitalize() for p in parts[1:])


def normalize_key(s):
    """Lowercase and remove non-alphanumeric for case-insensitive matching."""
    return re.sub(r"[^a-z0-9]", "", s.lower())


def resolve_service_section_key(data, service):
    """
    Case-insensitively find the service section key in the data dict.
    Returns the original key name (e.g., 'ChannelService') or None.
    """
    if not isinstance(data, dict):
        return None
    target_norm = normalize_key(service)
    candidates = [
        service,
        service_to_section(service),
        service_to_camel(service),
        service_to_camel(service) + "Service",
        service.replace("-", ""),
        service.replace("-", "") + "service",
    ]
    for candidate in candidates:
        if candidate in data:
            return candidate
    for key in data.keys():
        if normalize_key(key) == target_norm:
            return key
        if normalize_key(key) == normalize_key(service + "Service"):
            return key
    return None


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
        if isinstance(cur, list):
            row, _ = cur.lc.item(leaf)
            return row
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
        or s[0] in ('"', "'", "{", "[", "|", ">", "&", "*", "!", "%", "@", "`")
        or ":" in s
        or "#" in s
        or s.lower() in ("true", "false", "null", "yes", "no", "on", "off")
        or s != s.strip()
    )
    if needs_quote:
        escaped = s.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return s


def patch_line(original_line, leaf_key, new_value):
    leaf_str = str(leaf_key)
    pattern = re.compile(
        r"^(\\s*" + re.escape(leaf_str) + r"\\s*:\\s*)([^#\\r\\n]*)(.*)",
        re.DOTALL,
    )
    line_body = original_line.rstrip("\r\n")
    match = pattern.match(line_body)
    if not match:
        return None
    prefix = match.group(1)
    suffix = match.group(3)
    ending = "\r\n" if original_line.endswith("\r\n") else "\n"
    new_val_str = scalar_to_str(new_value)
    comment_part = (" " + suffix.strip()) if suffix.strip() else ""
    return prefix + new_val_str + comment_part + ending


def get_child_indentation(lines, parent_line_no, parent_indent):
    """
    Scan lines after parent_line_no until indentation <= parent_indent.
    Find the first non-empty, non-comment line that contains a colon (':')
    and return its leading spaces. If no such child exists, return parent_indent + 2.
    """
    child_indent = parent_indent + 2
    index = parent_line_no + 1
    while index < len(lines):
        line = lines[index]
        if not line.strip():
            index += 1
            continue
        line_indent = len(line) - len(line.lstrip())
        if line_indent <= parent_indent:
            break
        if ":" in line and not line.lstrip().startswith("#"):
            child_indent = line_indent
            break
        index += 1
    return child_indent


def insert_flat_key_at_level(lines, start_idx, base_indent, flat_key, value):
    """Insert a flat key at the end of the service section, using existing child indentation."""
    end_idx = start_idx + 1
    while end_idx < len(lines):
        line = lines[end_idx]
        if line.strip() and not line.startswith(" " * (base_indent + 2)):
            break
        end_idx += 1

    child_indent = base_indent + 2
    for index in range(start_idx + 1, end_idx):
        line = lines[index]
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


def insert_nested_key(lines, data, service_section_key, key_parts, value, separator="."):
    """
    Insert a key at the correct nesting level.
    If the parent path exists, insert inside the deepest existing parent.
    Else, fall back to flat insertion at service level.
    """
    section_re = re.compile(rf"^{re.escape(service_section_key)}:")
    start_idx = None
    for index, line in enumerate(lines):
        if section_re.match(line.lstrip()):
            start_idx = index
            break
    if start_idx is None:
        print(f"Cannot find service section '{service_section_key}'")
        return False

    base_indent = len(lines[start_idx]) - len(lines[start_idx].lstrip())

    if len(key_parts) == 1:
        return insert_flat_key_at_level(lines, start_idx, base_indent, key_parts[0], value)

    full_path_base = [service_section_key]
    deepest_parent_parts = []
    parent_line_no = None

    for index in range(len(key_parts) - 1, 0, -1):
        candidate_parts = full_path_base + key_parts[:index]
        line_no = get_node_line(data, candidate_parts)
        if line_no is not None:
            deepest_parent_parts = key_parts[:index]
            parent_line_no = line_no
            break

    if not deepest_parent_parts:
        print(f"No existing parent for '{'.'.join(key_parts)}'. Inserting as flat key.")
        return insert_flat_key_at_level(lines, start_idx, base_indent, separator.join(key_parts), value)

    parent_line = lines[parent_line_no]
    parent_indent = len(parent_line) - len(parent_line.lstrip())
    child_indent = get_child_indentation(lines, parent_line_no, parent_indent)

    end_idx = parent_line_no + 1
    end_section_idx = len(lines)
    for index in range(start_idx + 1, len(lines)):
        if lines[index].strip() and not lines[index].startswith(" " * (base_indent + 2)):
            end_section_idx = index
            break

    while end_idx < end_section_idx:
        line = lines[end_idx]
        if line.strip() and len(line) - len(line.lstrip()) <= parent_indent:
            break
        end_idx += 1

    leaf_part = key_parts[-1]
    new_line = f"{' ' * child_indent}{leaf_part}: {scalar_to_str(value)}\n"
    lines.insert(end_idx, new_line)
    print(f"Inserted '{leaf_part}' under '{'.'.join(deepest_parent_parts)}' in section '{service_section_key}'")
    return True


def apply_changes(target_file, ai_output_file, source_changes_file):
    if not target_file.exists():
        print(f"Target file not found: {target_file}")
        return 1

    with ai_output_file.open("r", encoding="utf-8") as file_obj:
        rows = json.load(file_obj)

    source_changes_lookup = {}
    if source_changes_file.exists():
        with source_changes_file.open("r", encoding="utf-8") as file_obj:
            for row in json.load(file_obj):
                source_changes_lookup[(row.get("service"), row.get("key"))] = row

    original_text = target_file.read_text(encoding="utf-8")
    yaml_parser = YAML(typ="rt")
    yaml_parser.preserve_quotes = True
    data = yaml_parser.load(original_text) or {}

    lines = original_text.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"

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

        rel_key = key
        for prefix in (
            f"{service_section_key}.",
            f"{service_to_section(service)}.",
            f"{service_to_camel(service)}.",
        ):
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
                (path, val)
                for path, val in iter_scalar_paths(service_root, [])
                if matches_tail(path, rel_parts) or path[-1:] == rel_parts[-1:]
            ]
            if old_value is not None:
                narrow = [candidate for candidate in candidates if candidate[1] == old_value]
                if narrow:
                    candidates = narrow
            if len(candidates) == 1:
                chosen_parts = candidates[0][0]
            elif len(candidates) > 1:
                paths_str = ", ".join(".".join(str(x) for x in candidate[0]) for candidate in candidates)
                print(f"Skipping ambiguous match for {service_section_key}.{rel_key}: [{paths_str}]")
                skipped += 1
                continue

        if chosen_parts is None:
            inserted = insert_nested_key(lines, data, service_section_key, rel_parts, value, separator=".")
            if inserted:
                applied += 1
                continue
            print(
                f"Skipping: path not found and insertion failed for "
                f"{service_section_key}.{'.'.join(str(part) for part in rel_parts)}"
            )
            skipped += 1
            continue

        full_parts = [service_section_key] + chosen_parts
        line_no = get_value_line(data, full_parts)

        if line_no is None or line_no >= len(lines):
            print(f"Skipping: could not locate line for {'.'.join(str(part) for part in full_parts)}")
            skipped += 1
            continue

        new_line = patch_line(lines[line_no], full_parts[-1], value)
        if new_line is None:
            print(f"Skipping: line regex did not match for {'.'.join(str(part) for part in full_parts)}")
            skipped += 1
            continue

        dotted = ".".join(str(part) for part in full_parts)
        print(f"Patching line {line_no + 1}: {dotted} = {json.dumps(value)}")
        lines[line_no] = new_line
        applied += 1

    if applied > 0:
        target_file.write_text("".join(lines), encoding="utf-8")
    else:
        print("No updates applied; chart/values.yaml left untouched.")

    print(f"Applied {applied} change(s), skipped {skipped} change(s).")
    return 0


def main():
    parser = argparse.ArgumentParser(description="Apply AI output changes into Helm values.yaml safely.")
    parser.add_argument(
        "--target-file",
        default="chart/values.yaml",
        help="Path to target values.yaml inside helm repo.",
    )
    parser.add_argument(
        "--ai-output-file",
        default="../ai_output.json",
        help="Path to ai_output.json generated by AI transform step.",
    )
    parser.add_argument(
        "--source-changes-file",
        default="../changes.json",
        help="Path to source changes.json artifact.",
    )
    args = parser.parse_args()

    target_file = Path(args.target_file)
    ai_output_file = Path(args.ai_output_file)
    source_changes_file = Path(args.source_changes_file)

    return_code = apply_changes(target_file, ai_output_file, source_changes_file)
    sys.exit(return_code)


if __name__ == "__main__":
    main()
