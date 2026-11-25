"""Deterministic, resumable VASP band workflow. No model/API key required."""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import numpy as np
from pymatgen.core import Structure
from pymatgen.io.vasp.inputs import Incar, Kpoints, Poscar, Potcar
from pymatgen.io.vasp.outputs import Vasprun
from pymatgen.symmetry.bandstructure import HighSymmKpath


class ToolError(ValueError):
    """Stable error identity; messages remain suitable for CLI users."""
    def __init__(self, code, message, next_action=None):
        super().__init__(message)
        self.error_code = code
        self.next_action = next_action


def save(path, data):
    path = Path(path)
    tmp = path.with_name(path.name + '.' + uuid4().hex + '.tmp')
    try:
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding='utf-8')
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)
        forget_digests()


# Thread-local per-call digest memo; None outside digest_memo(). Exposed via
# __getattr__('_digests') so existing diagnostics/tests can inspect the current
# thread's active memo without a shared module global race.
_digests_local = threading.local()


def _current_digests():
    return getattr(_digests_local, 'memo', None)


def __getattr__(name):
    if name == '_digests':
        return _current_digests()
    raise AttributeError(f'module {__name__!r} has no attribute {name!r}')


def file_identity(path):
    """What a memoized digest is bound to: the path and the file's stat at read time.

    Any write changes size, mtime or (kernel-set, not settable by utime) ctime, and
    an atomic replace changes the inode. remote_worker's worker_common computes the
    same tuple, so the worker and this engine can share one memo within a call.
    """
    st = os.stat(path)
    return (os.path.abspath(path), st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def _sha256_file(path):
    """Full streaming SHA-256, and the identity it is bound to (None if the file moved or changed)."""
    h = hashlib.sha256()
    with open(path, 'rb') as stream:
        opened, before = os.fstat(stream.fileno()), file_identity(path)
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
        still, after = os.fstat(stream.fileno()), file_identity(path)
    # The path still names the file that was read, and neither changed during the read.
    # (Both stats are compared: on Windows os.stat and os.fstat fill st_ctime differently.)
    unchanged = (before == after and (opened.st_dev, opened.st_ino) == before[1:3]
                 and all(getattr(opened, k) == getattr(still, k) for k in ('st_size', 'st_mtime_ns', 'st_ctime_ns')))
    return h.hexdigest(), before if unchanged else None


def digest(path):
    memo = _current_digests()
    if memo is None:
        return _sha256_file(path)[0]
    value = memo.get(file_identity(path))
    if value is None:
        value, identity = _sha256_file(path)
        if identity is not None:  # a file written during the read is never memoized
            memo[identity] = value
    return value


@contextlib.contextmanager
def digest_memo(memo=None):
    """Hash each unchanged file at most once inside one read/verify call.

    Every check still compares a full SHA-256; a repeat lookup of the same file
    returns the hash computed earlier in this call when the file's identity
    (file_identity) is unchanged, and hashes it again otherwise. The memo lives
    only inside this block -- one worker action or one local review -- and is
    never stored or reused across calls or processes. save() clears it, so a file
    this process rewrites is always hashed again.

    Remaining window, documented rather than closed: a same-size in-place rewrite
    by another process that lands within the filesystem's timestamp granularity of
    the first read (nanoseconds on ext4/xfs, coarser on some network filesystems)
    keeps the identity and is served the earlier hash. Without the memo that same
    write is equally missed if it lands just after the last read, so every
    verification is still a statement about the file at a point in this call.

    `memo` lets the remote worker share its own per-call memo with the engine.
    """
    previous = _current_digests()
    active = memo if memo is not None else ({} if previous is None else previous)
    _digests_local.memo = active
    try:
        yield active
    finally:
        _digests_local.memo = previous


def forget_digests():
    memo = _current_digests()
    if memo is not None:
        memo.clear()


# Parallelization-only INCAR keys a full-workflow stage may add at launch; see
# full_workflow.execution_tags. Nothing else may differ from the prepared files.
EXECUTION_TAGS = ('KPAR', 'NCORE')


def without_execution_lines(text):
    """INCAR text (str or bytes) with the execution-tag lines removed, all else untouched."""
    separator = b'=' if isinstance(text, bytes) else '='
    kept = [line for line in text.splitlines(keepends=True)
            if (line.split(separator, 1)[0].strip().decode() if isinstance(line, bytes)
                else line.split(separator, 1)[0].strip()) not in EXECUTION_TAGS]
    return text[:0].join(kept)


def prepared_matches(root, name, expected, config):
    """A manifest-hashed file still is what preparation wrote.

    For tasks prepared with execution tags, a stage INCAR is the prepared file
    plus the KPAR/NCORE lines its launch appended; the manifest keeps the hash of
    the prepared physics, so those lines alone are disregarded here.
    """
    path = root / name
    if digest(path) == expected:
        return True
    return (config.get('execution_tags') is not None and name.endswith('/INCAR')
            and hashlib.sha256(without_execution_lines(path.read_bytes())).hexdigest() == expected)


_event_lock = threading.Lock()


def event(root, action, **details):
    """Append one workflow event.

    The session is the join key to the agent's .ai-audit.jsonl. It arrives in the
    environment: set directly for an in-process call, and carried onto the
    compute node by sbatch --export for a submitted job. Like the timestamp it is
    written here rather than by callers, and it wins over any detail of the same
    name -- so it cannot be omitted, and a call site cannot relabel its own
    event. Non-string details are serialized rather than raising, because losing
    an event is worse than losing the type of one field.
    """
    entry = {'time': datetime.now(timezone.utc).isoformat(),
             'session': os.environ.get('VASP_AGENT_SESSION'),
             'action': action, **details}
    with _event_lock, (root / 'events.jsonl').open('a', encoding='utf-8') as out:
        out.write(json.dumps(entry, ensure_ascii=False, default=str) + '\n')


def prepare(args):
    if getattr(args, 'workflow', 'bands-only') == 'full':
        from vasp_agent.workflow.full_workflow import prepare_full
        return prepare_full(args)
    if getattr(args, 'potcar_library', None):
        raise ValueError('赝势库自动组装目前用于 full 模式；bands-only 请提供 --potcar。')
    root = Path(args.workdir).resolve()
    if root.exists() and any(root.iterdir()):
        raise ValueError('工作目录非空；请使用新目录，避免覆盖计算。')
    if args.encut <= 0 or args.ediff <= 0 or args.points < 2 or min(args.mesh) < 1 or args.symprec <= 0:
        raise ValueError('ENCUT、EDIFF 和网格必须为正；每段至少两个点。')
    source = Structure.from_file(args.structure)
    if not source.is_ordered:
        raise ValueError('不支持部分占位结构，请先确定有序模型。')
    if source.site_properties:
        raise ValueError('结构含站点属性（如磁矩/固定原子），第一版不自动丢弃，请先审核结构。')
    if len(source) > 1:
        distances = source.distance_matrix + np.eye(len(source)) * 1e9
        if distances.min() < 0.5:
            raise ValueError('发现小于 0.5 Å 的原子间距，请检查结构。')
    from vasp_agent.workflow.dimensionality import classify
    if classify(source)['dimension'] != '3D':
        # Standardization and a 3D path are wrong for a slab; the full workflow
        # keeps the cell and samples only the plane.
        raise ValueError('检测到真空层（低维体系）；bands-only 模式仅支持三维体相，二维 slab 请使用 full 模式。')
    # Use exactly the primitive basis for which the path is defined.
    path = HighSymmKpath(source, symprec=args.symprec)
    structure = path.prim
    poscar = Poscar(structure)
    potentials = None
    if args.potcar:
        potentials = Potcar.from_file(args.potcar)
        species = [p.element for p in potentials]
        if species != poscar.site_symbols:
            raise ValueError(f'POTCAR 元素顺序 {species} 与标准原胞 {poscar.site_symbols} 不一致。')
        if any(p.functional != 'PBE' for p in potentials):
            raise ValueError('第一版只支持 PBE 赝势。')
        if args.encut < max(float(p.enmax) for p in potentials):
            raise ValueError('ENCUT 小于 POTCAR 中最大 ENMAX。')
    root.mkdir(parents=True, exist_ok=True)
    base = dict(ENCUT=args.encut, EDIFF=args.ediff, PREC='Accurate', GGA='PE',
                ISPIN=1, ISMEAR=0, SIGMA=0.05, LREAL=False, LASPH=True,
                LMAXMIX=6, NELM=150, NSW=0, IBRION=-1, LWAVE=False,
                ALGO='Normal', ISTART=0)
    if getattr(args, 'nbands', None) is not None:
        if args.nbands < 1:
            raise ValueError('NBANDS 必须为正整数。')
        base['NBANDS'] = args.nbands
    for stage in ('scf', 'bands'):
        folder = root / stage
        folder.mkdir()
        poscar.write_file(folder / 'POSCAR')
        Incar({**base, 'ICHARG': 2 if stage == 'scf' else 11,
               'LCHARG': stage == 'scf'}).write_file(folder / 'INCAR')
        if potentials is not None:
            shutil.copyfile(args.potcar, folder / 'POTCAR')
    Kpoints.gamma_automatic(args.mesh).write_file(root / 'scf/KPOINTS')
    Kpoints.automatic_linemode(args.points, path).write_file(root / 'bands/KPOINTS')
    shutil.copyfile(args.structure, root / 'original.structure')
    files = [str(f.relative_to(root)).replace('\\', '/') for stage in ('scf', 'bands')
             for f in (root / stage).iterdir()]
    manifest = dict(schema=1, scope='3D nonmagnetic PBE, fixed supplied geometry',
                    formula=structure.composition.reduced_formula,
                    source_atoms=len(source), primitive_atoms=len(structure),
                    potential_order=poscar.site_symbols, kpath=path.kpath['path'],
                    hashes={f: digest(root / f) for f in files},
                    ready=potentials is not None,
                    notes=['Input geometry is not relaxed by this program.',
                           'ENCUT and k mesh are starting values, not convergence-tested.',
                           'Path gap is not proof of the global fundamental gap.'])
    save(root / 'manifest.json', manifest)
    event(root, 'prepared', ready=manifest['ready'])
    print(json.dumps(manifest, indent=2, ensure_ascii=False))


def verify(root):
    manifest = json.loads((root / 'manifest.json').read_text(encoding='utf-8'))
    if not manifest['ready']:
        raise ValueError('这是无 POTCAR 的预览。请用 --potcar 在新目录重新 prepare。')
    config = json.loads((root / 'config.json').read_text(encoding='utf-8')) if (root / 'config.json').is_file() else {}
    for name, expected in manifest['hashes'].items():
        if not (root / name).is_file() or not prepared_matches(root, name, expected, config):
            raise ValueError(f'输入文件已改变：{name}；请建立新工作目录。')
    if (root / 'scan-verification.json').exists():
        from vasp_agent.workflow.scan_config import verify_scan_inputs
        verify_scan_inputs(root)
    return manifest


def _recorded_execution_tags(folder):
    """Parallelization tags written by full_workflow.stage into execution.json, or None."""
    path = folder / 'execution.json'
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    tags = data.get('tags') if isinstance(data, dict) else None
    return tags if isinstance(tags, dict) else None


def read_result(folder):
    # pymatgen populates efermi from the DOS element only when parse_dos=True.
    result = Vasprun(folder / 'vasprun.xml', parse_dos=True,
                     parse_projected_eigen=False, parse_potcar_file=False)
    if not result.converged_electronic:
        raise ValueError(f'{folder.name} 电子计算未收敛，停止后续步骤。')
    if result.efermi is None or not np.isfinite(result.efermi):
        raise ValueError('输出缺少有效费米能级。')
    expected_structure = Poscar.from_file(folder / 'POSCAR').structure
    if (result.initial_structure.species != expected_structure.species or
        not np.allclose(result.initial_structure.lattice.matrix, expected_structure.lattice.matrix, atol=1e-5) or
        not np.allclose(result.initial_structure.frac_coords, expected_structure.frac_coords, atol=1e-5)):
        raise ValueError(f'{folder.name} 输出结构与输入不一致。')
    execution_tags = None
    for key, expected in Incar.from_file(folder / 'INCAR').items():
        if key in result.incar:
            if result.incar[key] != expected:
                raise ValueError(f'{folder.name} 输出参数 {key} 与输入不一致。')
            continue
        if key in EXECUTION_TAGS:
            # VASP 5.4.4 reads KPAR/NCORE during MPI init before opening
            # vasprun.xml and omits them from <incar>; accept only when
            # execution.json recorded the exact same tag value for this stage.
            if execution_tags is None:
                execution_tags = _recorded_execution_tags(folder) or {}
            recorded = execution_tags.get(key)
            if type(recorded) is int and recorded == expected:
                continue
        raise ValueError(f'{folder.name} 输出参数 {key} 与输入不一致。')
    return result


def execute(root, stage, command, event_root=None):
    folder = root / stage
    stamp = folder / 'complete.json'
    if stamp.exists():
        recorded = json.loads(stamp.read_text())
        if recorded['xml_sha256'] != digest(folder / 'vasprun.xml'):
            raise ValueError(f'{stage} 输出与完成记录不一致。')
        result = read_result(folder)
        if stage == 'scf' and recorded['charge_sha256'] != digest(folder / 'CHGCAR'):
            raise ValueError('SCF 电荷文件已改变。')
        return result
    # Never silently overwrite an interrupted or externally produced calculation.
    if any((folder / name).exists() for name in ('vasprun.xml', 'OUTCAR', 'stdout.log')):
        raise ValueError(f'{stage} 存在未验收输出，请检查日志并在新目录重建任务。')
    event(event_root or root, 'started', stage=stage, command=command)
    with (folder / 'stdout.log').open('w', encoding='utf-8') as out:
        subprocess.run(command, cwd=folder, stdout=out, stderr=subprocess.STDOUT,
                       check=True, shell=False)
    result = read_result(folder)
    record = {'xml_sha256': digest(folder / 'vasprun.xml')}
    if stage == 'scf':
        charge = folder / 'CHGCAR'
        if not charge.is_file() or charge.stat().st_size == 0:
            raise ValueError('SCF 未生成有效 CHGCAR。')
        record['charge_sha256'] = digest(charge)
    save(stamp, record)
    event(event_root or root, 'completed', stage=stage)
    return result


def validate_band_data(result):
    from vasp_agent.workflow.band_export import validate_band_data as _validate
    return _validate(result)


def export_bands(bs, root, metadata):
    from vasp_agent.workflow.band_export import export_bands as _export
    return _export(bs, root, metadata)


def analyze(root):
    manifest_path = root / 'manifest.json'
    if json.loads(manifest_path.read_text(encoding='utf-8')).get('workflow') == 'full':
        from vasp_agent.workflow.full_workflow import analyze_full
        return analyze_full(root)
    manifest = verify(root)
    scf = read_result(root / 'scf')
    bands = read_result(root / 'bands')
    validation = validate_band_data(bands)
    bs = bands.get_band_structure(kpoints_filename=str(root / 'bands/KPOINTS'),
                                 line_mode=True, efermi=float(scf.efermi))
    if not any(np.any(values > bs.efermi) for values in bs.bands.values()):
        raise ValueError('没有费米能级以上的能带，无法报告带隙；请增加能带数。')
    report = export_bands(bs, root, {'formula': manifest['formula'], 'source': 'VASP vasprun.xml',
                                    'vasp_version': bands.vasp_version, 'band_validation': validation})
    event(root, 'analyzed')
    print(json.dumps(report, indent=2, ensure_ascii=False))


def run(args):
    root = Path(args.workdir).resolve()
    manifest = verify(root)
    command = args.command
    if command and command[0] == '--':
        command = command[1:]
    if not command:
        raise ValueError('请在 -- 后提供 VASP 启动命令，例如 srun vasp_std。')
    if manifest.get('workflow') == 'full' and json.loads((root / 'config.json').read_text()).get('submission_mode', 'allocation') == 'array':
        raise ValueError('数组模式必须使用 run-array-test，禁止通过普通 run 启动。')
    if not shutil.which(command[0]):
        raise ValueError(f'找不到启动命令：{command[0]}')
    if manifest.get('review_required'):
        from vasp_agent.workflow.input_review import require_review
        require_review(root)
    environment_file = root / 'environment-review.json'
    if environment_file.exists():
        environment = json.loads(environment_file.read_text(encoding='utf-8'))
        executable = shutil.which('vasp_std')
        if not executable or digest(Path(executable)) != environment['executable_sha256']:
            raise ValueError('计算节点VASP与准备阶段环境指纹不同，停止执行。')
    # Exclusive lock prevents simultaneous schedulers from launching duplicate jobs.
    lock = root / '.agent.lock'
    with lock.open('x'):
        pass
    try:
        if manifest.get('workflow') == 'full':
            from vasp_agent.workflow.full_workflow import run_full
            return run_full(root, command)
        execute(root, 'scf', command)
        target = root / 'bands/CHGCAR'
        if not target.exists():
            shutil.copyfile(root / 'scf/CHGCAR', target)
        if digest(target) != digest(root / 'scf/CHGCAR'):
            raise ValueError('能带 CHGCAR 与自洽结果不一致。')
        execute(root, 'bands', command)
        analyze(root)
    except Exception as exc:
        event(root, 'failed', error=str(exc))
        state_path = root / 'workflow-state.json'
        if manifest.get('workflow') == 'full' and state_path.exists():
            state = json.loads(state_path.read_text(encoding='utf-8'))
            save(state_path, {**state, 'status': 'failed', 'error': str(exc)})
        raise
    finally:
        lock.unlink()


def main(chat_handler=None):
    if len(sys.argv) > 1 and sys.argv[1] == 'chat':
        if chat_handler is None:
            raise SystemExit('请使用 vasp-ai 启动对话模式。')
        sys.argv.pop(1)
        return chat_handler()
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    prep = sub.add_parser('prepare', help='准备完整计算流程；无 POTCAR 时仅预览')
    prep.add_argument('structure')
    prep.add_argument('workdir')
    prep.add_argument('--potcar', help='按元素顺序拼接的、本地已有 PBE POTCAR')
    prep.add_argument('--potcar-library', help='完整流程：按元素自动拼接的本地 PBE 库目录')
    prep.add_argument('--mesh', nargs=3, type=int, default=[8, 8, 8])
    prep.add_argument('--encut', type=float, default=520)
    prep.add_argument('--ediff', type=float, default=1e-7)
    prep.add_argument('--points', type=int, default=40)
    prep.add_argument('--symprec', type=float, default=0.01)
    prep.add_argument('--band-path', help='完整流程：操作者指定能带路径，如 "Γ-X-M-Γ|Z-R@60"；默认自动路径')
    prep.add_argument('--nbands', type=int, help='可选：显式指定能带数，包含足够的空带')
    prep.add_argument('--workflow', choices=['full', 'bands-only'], default='full',
                      help='默认完整流程；bands-only 用于已优化结构')
    prep.add_argument('--relax-mode', choices=['cell', 'ions'], default='cell')
    prep.add_argument('--force-tol', type=float, default=0.01, help='最大力阈值 eV/Å')
    prep.add_argument('--stress-tol', type=float, default=1.0, help='最大应力分量阈值 kbar，cell 模式')
    prep.add_argument('--energy-tol', type=float, default=0.001, help='收敛测试能量差 eV/atom')
    prep.add_argument('--encuts', type=float, nargs=3, help='显式三级 ENCUT 扫描')
    prep.add_argument('--meshes', type=json.loads, help='显式三级网格 JSON，如 [[12,12,12],[14,14,14],[16,16,16]]')
    prep.add_argument('--nsw', type=int, default=200)
    prep.add_argument('--parallel-tests', type=int, default=1, help='独立测试并发数 1–6；大于 1 时需 Slurm')
    prep.add_argument('--cores-per-test', type=int, default=8, help='并发时每个测试的 MPI 进程数')
    prep.add_argument('--submission-mode', choices=['allocation', 'array'], default='allocation',
                      help='array：每个收敛测试使用独立 Slurm allocation')
    runner = sub.add_parser('run', help='运行并验收前置流程 → SCF → 能带 → 报告')
    runner.add_argument('workdir')
    runner.add_argument('command', nargs=argparse.REMAINDER)
    array_runner = sub.add_parser('run-array-test', help='运行 SLURM_ARRAY_TASK_ID 指定的单个收敛测试并验收数组')
    array_runner.add_argument('workdir')
    array_runner.add_argument('command', nargs=argparse.REMAINDER)
    for action in ('analyze', 'status'):
        sub.add_parser(action).add_argument('workdir')
    args = parser.parse_args()
    try:
        if args.action == 'prepare':
            prepare(args)
        elif args.action == 'run':
            run(args)
        elif args.action == 'run-array-test':
            from vasp_agent.workflow.convergence_array import run_array_test
            command = args.command[1:] if args.command and args.command[0] == '--' else args.command
            run_array_test(Path(args.workdir).resolve(), command)
        elif args.action == 'analyze':
            analyze(Path(args.workdir).resolve())
        else:
            root = Path(args.workdir)
            print((root / 'manifest.json').read_text(encoding='utf-8'))
            for stamp in sorted(root.rglob('complete.json')):
                print(f'{stamp.parent.relative_to(root)}: execution validated')
            if (root / 'workflow-state.json').exists():
                print((root / 'workflow-state.json').read_text(encoding='utf-8'))
            if (root / 'events.jsonl').exists():
                print((root / 'events.jsonl').read_text(encoding='utf-8'))
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f'错误：{exc}\n')


if __name__ == '__main__':
    main()
