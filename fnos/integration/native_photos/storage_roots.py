"""Current native fnOS storage mounts, including hot-plug and remote volumes."""
from pathlib import Path
import re


def mounted_data_roots():
    roots = []
    for line in Path('/proc/self/mountinfo').read_text().splitlines():
        raw = line.split()[4]
        path = re.sub(r'\\([0-7]{3})', lambda m: chr(int(m[1], 8)), raw)
        if (re.fullmatch(r'/vol[1-9][0-9]*', path)
                or re.fullmatch(r'/vol0[012]/[^/]+(?:/[^/]+)*', path)):
            roots.append(path)
    # An empty list denies requests until native storage becomes available.
    # Do not kill the AI worker during storage initialization or disconnection.
    return sorted(set(roots))
