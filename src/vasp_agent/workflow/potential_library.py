"""Local PBE library selection and byte-preserving POTCAR assembly."""
import hashlib
import re
import tomllib
from pathlib import Path

from pymatgen.io.vasp.inputs import Potcar


def select_potentials(library, elements, potential_variants=None):
    root = Path(library).resolve(strict=True)
    mapping_file = root / 'potentials.toml'
    mapping = tomllib.loads(mapping_file.read_text(encoding='utf-8-sig')).get('potentials', {}) if mapping_file.exists() else {}
    if potential_variants is not None:
        if not isinstance(potential_variants, list):
            raise ValueError('赝势变体必须是 element/variant 对象列表。')
        seen = set()
        for item in potential_variants:
            if not isinstance(item, dict) or set(item) != {'element', 'variant'}:
                raise ValueError('赝势变体需要 element 和 variant。')
            element = item['element']
            if not isinstance(element, str) or element not in elements or element in seen:
                raise ValueError('赝势变体元素不在结构中或重复。')
            seen.add(element)
            mapping[element] = item['variant']
    selected = []
    for element in elements:
        variant = mapping.get(element, element)
        if not isinstance(variant, str) or not re.fullmatch(re.escape(element) + r'(?:_[A-Za-z0-9]+)*', variant):
            raise ValueError(f'{element} 的赝势映射无效。')
        path = (root / variant / 'POTCAR').resolve()
        if not path.is_relative_to(root):
            raise ValueError('赝势路径超出库目录。')
        if not path.is_file():
            alternatives = sorted(p.parent.name for p in root.glob(f'{element}_*/POTCAR'))
            raise ValueError(f'缺少 {variant}/POTCAR；可见变体：{alternatives}。请补齐文件或在 potentials.toml 明确指定。')
        selected.append((element, variant, path))
    return selected


def assemble_potentials(library, elements, potential_variants=None):
    chunks, records = [], []
    for element, variant, path in select_potentials(library, elements, potential_variants):
        data = path.read_bytes()
        try:
            pots = Potcar.from_str(data.decode('utf-8'))
            if len(pots) != 1 or pots[0].element != element or pots[0].functional != 'PBE':
                raise ValueError('element/functional mismatch')
        except Exception:
            raise ValueError(f'{variant}/POTCAR 不是所需元素的单个有效 PBE 赝势。') from None
        if not data.endswith(b'\n'):
            data += b'\n'
        chunks.append(data)
        records.append({'element': element, 'variant': variant, 'source': str(path),
                        'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                        'titel': pots[0].TITEL, 'zval': float(pots[0].ZVAL) if hasattr(pots[0], 'ZVAL') else None, 'enmax_eV': float(pots[0].enmax)})
    return b''.join(chunks), records
