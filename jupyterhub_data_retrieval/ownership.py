import hashlib
import re
import string


_escape_slug_safe_chars = set(string.ascii_lowercase + string.digits)

_alpha_lower = tuple(string.ascii_lowercase)
_alphanum_lower = tuple(string.ascii_lowercase + string.digits)

# patterns _do not_ need to cover length or start/end conditions,
# which are handled separately
_object_pattern = re.compile(r"^[a-z0-9\-]+$")
# match anything that's not lowercase alphanumeric (will be stripped, replaced with '-')
_non_alphanum_pattern = re.compile(r"[^a-z0-9]+")

_hash_length = 8


def escape_slug(name):
    """Generate a slug with the legacy system: unsafe characters become -XX hex escapes"""
    chars = []
    for c in name:
        if c in _escape_slug_safe_chars:
            chars.append(c)
        else:
            chars.extend(f"-{byte:X}" for byte in c.encode("utf8"))
    return "".join(chars).lower()


def _extract_safe_name(name, max_length):
    """Generate safe substring of a name"""
    safe_name = _non_alphanum_pattern.sub("-", name.lower())
    safe_name = safe_name.lstrip("-")[:max_length].rstrip("-")
    if safe_name and not safe_name.startswith(_alpha_lower):
        safe_name = "x-" + safe_name[: max_length - 2]
    if not safe_name:
        safe_name = "x"
    return safe_name


def strip_and_hash(name, max_length=32):
    """Generate an always-safe, unique string for any input"""
    name_length = max_length - (_hash_length + 3)
    name_hash = hashlib.sha256(name.encode("utf8")).hexdigest()[:_hash_length]
    safe_name = _extract_safe_name(name, name_length)
    return f"{safe_name}---{name_hash}"


def is_valid_object_name(s):
    """RFC 1035 and 1123 DNS label: 63 chars, lowercase letters, digits and '-'"""
    return (
        1 <= len(s) <= 63
        and s.startswith(_alpha_lower)
        and s.endswith(_alphanum_lower)
        and bool(_object_pattern.match(s))
    )


def safe_slug(name):
    """Always generate a safe slug"""
    # don't accept any names that could collide with the safe slug
    if "--" not in name and is_valid_object_name(name):
        return name
    return strip_and_hash(name)


def archive_key(object_name):
    """Archive file name without its directory or extension, lowercased"""
    basename = object_name.rsplit("/", 1)[-1]
    return re.sub(r"(\.tar\.gz|\.zip)$", "", basename).lower()


def candidate_keys(email):
    """Archive keys that belong to an email address.

    The escaped username and email (the names the GitHub action accepts),
    plus the raw username, which the oldest (.zip) archives use.
    """
    username = email.split('@')[0].lower()
    email = email.lower()
    return {
        username,
        escape_slug(username),
        safe_slug(username),
        escape_slug(email),
        safe_slug(email),
    }
