import argparse
import errno
import hashlib
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path


logger = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(
        description = 'Compile out-of-tree QMK keyboard/layouts.',
        epilog = 'Additional arguments are passed to QMK\'s make command.'
    )

    parser.add_argument('keyboard',
        metavar='KEYBOARD', help='out-of-tree keyboard primary C file or QMK keyboard name')
    parser.add_argument('layout', nargs='?', default='default',
        metavar='LAYOUT', help='out-of-tree layout directory or QMK layout/keymap name')

    parser.add_argument('--qmk', dest='qmk_home',
        metavar='QMK_DIR', help='QMK install directory')
    parser.add_argument('--tmp', dest='qmk_tmp',
        metavar='TMP_DIR', help='temporary build directory')
    
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--flash', nargs='?', default=False, const=True, choices=['l','r'],
        help='flash firmware to device after compilation')
    group.add_argument('--into',
        metavar='DEST', help='directory or filename to copy compiled .hex to')

    parser.add_argument('--fresh', action='store_true',
        help='start with a clean install')
    
    parser.add_argument('-v', '--verbose', action='count', default=0)

    try:
        return _main(*parser.parse_known_args())
    except KeyboardInterrupt:
        return 1



def _main(args, extra_args):

    logging.basicConfig(level=VERBOSITY(args.verbose), format='%(message)s')

    pretty_path = PathFormatter({
        Path.cwd() : '.',
        Path.home(): '~',
    })


    if args.qmk_home:
        qmk_home = Path(args.qmk_home).resolve()
        if not qmk_home.is_dir():
            logger.error('specified QMK install `%s` is not a directory', pretty_path(qmk_home))
            return 1
        logger.info('using QMK from `%s`', qmk_home)
    elif 'QMK_HOME' in os.environ:
        qmk_home = Path(os.environ['QMK_HOME']).resolve()
        if not qmk_home.is_dir():
            logger.error('$QMK_HOME `%s` is not a directory', pretty_path(qmk_home))
            return 1
        logger.info('using $QMK_HOME `%s`', pretty_path(qmk_home))
    else:
        logger.error('no QMK directory set, specify using `--qmk` argument or `QMK_HOME` environment variable')
        return 1



    if args.qmk_tmp:
        qmk_tmp = Path(args.qmk_tmp)
    else:
        qmk_tmp = Path(tempfile.gettempdir()) / 'qmk-hermit'

    qmk_tmp /= path_hash(qmk_home) # unique subdir per source install

    logger.info('using `%s` as temporary QMK', pretty_path(qmk_tmp))

    if args.fresh:
        shutil.rmtree(qmk_tmp, ignore_errors=True)


    keyboard_path = Path(args.keyboard).resolve()
    if keyboard_path.is_file():
        logger.info('using out-of-tree keyboard `%s`', pretty_path(keyboard_path))
        usr_keyboard_name = keyboard_path.stem
        tmp_keyboard_name, tmp_keyboard_dir = setup_temporary_keyboard(keyboard_path.parent, usr_keyboard_name, qmk_tmp)

        for line in PathTree(tmp_keyboard_dir).pretty_lines(path_formatter=pretty_path):
            logger.log(VERBOSITY(1), line)

    else:
        if is_valid_qmk_keyboard(qmk_home, args.keyboard):
            logger.info('using QMK keyboard `%s`', args.keyboard)
            usr_keyboard_name = tmp_keyboard_name = args.keyboard
        else:
            logger.error('`%s` is not a valid keyboard', args.keyboard)
            return 1


    keyboard_rules = parse_keyboard_rules(qmk_tmp, tmp_keyboard_name)


    layout_path = Path(args.layout).resolve()
    if layout_path.is_dir():
        logger.info('using out-of-tree layout `%s`', pretty_path(layout_path))

        layout_type = guess_layout_type(layout_path)
        if not layout_type:
            logger.error('could not guess layout type')
            return 1
        logger.log(VERBOSITY(1), 'guessed `%s` layout type', layout_type)

        supported_layouts = set(keyboard_rules.get('LAYOUTS', '').split())
        if not supported_layouts:
            logger.error('keyboard does not supports layouts')
            return 1
        elif not layout_type in supported_layouts:
            logger.error('keyboard does not support `%s` layouts', layout_type)
            return 1
        else:
            logger.log(VERBOSITY(1), 'keyboard supports layouts: %s', supported_layouts)


        usr_keymap_name = layout_path.stem
        tmp_keymap_name, tmp_keymap_dir = setup_temporary_layout(layout_path, layout_type, qmk_tmp)

        for line in PathTree(tmp_keymap_dir).pretty_lines(path_formatter=pretty_path):
            logger.log(VERBOSITY(1), line)
    else:
        #TODO check if valid layout/keymap ?
        logger.info('using QMK layout `%s`', args.layout)
        usr_keymap_name = tmp_keymap_name = args.layout



    logger.log(VERBOSITY(1), 'setting up temporary QMK directory')
    setup_temporary_qmk(qmk_home, qmk_tmp)



    logger.info('QMK build...')

    make_targets, make_args = partition(lambda a:a.startswith(':'), extra_args)

    def make_target():
        yield tmp_keyboard_name
        yield tmp_keymap_name

        if args.flash in ('l', 'r'):
            bootloader = keyboard_rules.get('BOOTLOADER', '').lower()
            if 'dfu' in bootloader:
                if args.flash=='r': yield 'dfu-split-right'
                else              : yield 'dfu-split-left'
            elif 'caterina' in bootloader:
                if args.flash=='r': yield 'avrdude-split-right'
                else              : yield 'avrdude-split-left'
            else:
                logger.warning('no left/right specific flash target for `%s` bootloader', bootloader)
                yield 'flash'
        elif args.flash:
            yield 'flash'

        yield from (target.lstrip(':') for target in make_targets)

    make_cmd = ['make', ':'.join(make_target()), *make_args]

    logger.log(VERBOSITY(1), 'make command: `%s`', subprocess.list2cmdline(make_cmd))

    process = subprocess.Popen(make_cmd, cwd=qmk_tmp, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    quote_process_output(process)
    process.wait()
    
    if process.returncode == 0:
        logger.info('QMK build done.')
    else:
        logger.error('QMK build failed.')
        return 1

    if args.into:
        into = Path(args.into)
        expected_fn = f'{tmp_keyboard_name}_{tmp_keymap_name}.hex'.replace(os.path.sep,'_')
        output_path = qmk_tmp / expected_fn
        if output_path.is_file():
            if into.is_dir():
                output_copy_path = into / f'{usr_keyboard_name}_{usr_keymap_name}.hex'
            else:
                output_copy_path = into
            logger.info('copying output to `%s`', pretty_path(output_copy_path))
            copy_file(output_path, output_copy_path)
        else:
            logger.error('output file `%s` not found', pretty_path(output_path))
            return 1




def setup_temporary_qmk(qmk_home, qmk_tmp):

    # make `.build` an actual dir so we don't compile in actual source tree
    make_dir(qmk_tmp / '.build')

    # make `keyboards` and `layouts` actual dirs because we need to add temporary sources
    make_dir(qmk_tmp / 'keyboards')
    make_dir(qmk_tmp / 'layouts')

    # copy `bin/qmk` because it uses `realpath` internally
    copy_file(qmk_home / 'bin/qmk', qmk_tmp / 'bin/qmk')

    # create `quantum/version.h` as an actual file because it is written to on compile
    touch_file(qmk_tmp / 'quantum/version.h')


    # symlimk everything else
    q = list(qmk_home.iterdir())
    while q:
        src = q.pop(0)
        dst = qmk_tmp / src.relative_to(qmk_home)
        if dst.is_dir():
            q += src.iterdir()
        elif not dst.exists():
            make_symlink(src, dst)



def setup_temporary_keyboard(src_dir, src_name, qmk_tmp, subdir='hermit'):

    dst_name = path_hash(src_dir)
    dst_dir = qmk_tmp / 'keyboards' / subdir / dst_name

    def renames(fn):
        if fn == f'{src_name}.c':
            yield f'{dst_name}.c'
            yield fn
        elif fn == f'{src_name}.h':
            yield f'{dst_name}.h'
            yield fn
        else:
            m = re.match(r'keymap(?:[-_ ]+(.+))?\.(c|h)', fn)
            if m:
                name,ext = m.group(1) or 'default', m.group(2)
                yield Path('keymaps') / name / f'keymap.{ext}'
            else:
                yield fn

    shutil.rmtree(dst_dir, ignore_errors=True)

    for src_fn in os.listdir(src_dir):
        for dst_fn in renames(src_fn):
            make_symlink(src_dir/src_fn, dst_dir/dst_fn)

    return os.path.join(subdir, dst_name), dst_dir



def setup_temporary_layout(src_dir, layout_type, qmk_tmp, subdir='hermit'):

    dst_name = path_hash(src_dir)
    dst_dir = qmk_tmp / 'layouts' / subdir / layout_type / dst_name

    shutil.rmtree(dst_dir, ignore_errors=True)

    for fn in os.listdir(src_dir):
        src = src_dir / fn
        dst = dst_dir / fn
        make_symlink(src, dst)

    return dst_name, dst_dir



def is_valid_qmk_keyboard(qmk_home, keyboard_name):
    return any(qmk_home.glob('keyboards/**/' + keyboard_name))



def guess_layout_type(src_dir):
    def find_occurences():
        for glob in ('**/*.c', '**/*.h'):
            for fn in src_dir.glob(glob):
                for line in open(fn):
                    yield from re.findall(r'LAYOUT_(.+)\s*\(', line)

    for k,n in Counter(find_occurences()).most_common(1):
        return k



def parse_keyboard_rules(qmk_home, keyboard_name):
    def find_rules():
        for parent in reversed([keyboard_name, *Path(keyboard_name).parents]):
            rules = qmk_home / 'keyboards' / parent / 'rules.mk'
            if rules.is_file():
                yield rules

    parsed_rules = {}
    for path in find_rules():
        parse_rulesmk(open(path), parsed_rules)

    return parsed_rules


def parse_rulesmk(lines, initial=None):
    parsed = initial if initial != None else {}

    ops = {
        '=' : lambda old,new: new,
        ':=': lambda old,new: new,
        '+=': lambda old,new: (old + ' ' + new) if old else new,
        '?=': lambda old,new: new if old else old,
    }

    regex = re.compile(r'(\w+)\s*(' + '|'.join(map(re.escape, ops)) + r')\s*(.+)')
    for line in makefile_lines(lines):
        m = regex.match(line)
        if m:
            name, op, val = m.groups()
            parsed[name] = ops[op](parsed.get(name), val)

    return parsed


def makefile_lines(lines):
    def stripped_lines(lines):
        for line in lines:
            if '#' in line:
                line = line[:line.index('#')]
            yield line.rstrip()
    
    def continued_lines(lines):
        lines = iter(lines)
        try:
            while True:
                line = next(lines)
                cont_line = line
                while line.endswith('\\'):
                    cont_line = line.rstrip('\\')
                    line = next(lines)
                    cont_line += '\n' + line
                yield line
        except StopIteration:
            pass

    return continued_lines(stripped_lines(lines))





def path_hash(path, length=8):
    return hashlib.shake_128(str(path.resolve()).encode()).hexdigest(length//2)


def make_symlink(src, dst):
    logger.log(VERBOSITY(2), 'symlink `%s` -> `%s`', dst, src)
    try:
        os.makedirs(dst.parent, exist_ok=True)
        os.symlink(src, dst)
    except OSError as e:
        if e.errno == errno.EEXIST and dst.is_symlink():
            os.remove(dst)
            os.symlink(src, dst)
        else:
            raise


def make_dir(dst):
    if not dst.exists():
        logger.log(VERBOSITY(2), 'mkdir `%s`', dst)
        os.makedirs(dst, exist_ok=True)


def copy_file(src, dst):
    if src.exists():
        logger.log(VERBOSITY(2), 'copy `%s` <- `%s`', dst, src)
        make_dir(dst.parent)
        if dst.is_file():
            os.remove(dst)
        shutil.copy(src, dst)


def touch_file(dst):
    logger.log(VERBOSITY(2), 'touch `%s`', dst)
    make_dir(dst.parent)
    dst.touch()



class PathTree:
    def __init__(self, path, parent=None, max_depth=float('inf'), sort_key=None):
        self.path = Path(path)
        if self.path.is_dir():
            if max_depth > 0:
                self.children = [PathTree(child, self, max_depth=max_depth-1)
                                 for child in sorted(self.path.iterdir(), key=sort_key)]
            else:
                self.children = [PathTree('…', self)]
        else:
            self.children = []
        self.parent = parent

    @property
    def is_last(self):
        return not self.parent or self.parent.children[-1] is self

    @property
    def basename(self):
        name = self.path.stem + self.path.suffix
        return (name + '/') if self.path.is_dir() else name


    def traverse(self, path=None):
        yield self, path
        for child in self.children:
            yield from child.traverse(path=(path+[self]) if path else [self])


    def pretty_lines(self, path_formatter=None):
        if not path_formatter:
            path_formatter = str

        def make_indents(node, parents=None):
            if parents:
                for parent in parents[1:]:
                    yield '    ' if parent.is_last else '│   '
            yield '└──' if node.is_last else '├──'

        def name(node):
            name = node.basename
            if node.path.is_symlink():
                return name + ' -> ' + path_formatter(node.path.resolve())
            else:
                return name

        yield path_formatter(self.path)
        for node,parents in self.traverse():
            if parents:
                yield ''.join(make_indents(node, parents)) + ' ' + name(node)


class PathFormatter:
    def __init__(self, aliases):
        self.aliases = dict(aliases)

    def __call__(self, path):
        for actual,alias in self.aliases.items():
            try:
                return os.path.join(alias, path.relative_to(Path(actual)))
            except ValueError:
                pass # not a subpath, fine
        return str(path)



def quote_stream_lines(stream, prefix='', suffix='', tmp_prefix='', read_by=1):
    prev = '\n'
    for c in iter(lambda: stream.read(read_by), ''):
        if prefix and prev == '\n':
            if tmp_prefix:
                yield '\r'
            yield prefix
        if suffix and c == '\n':
            yield suffix
        yield c
        if prefix and tmp_prefix and c == '\n':
            yield tmp_prefix
        prev = c


def quote_process_output(process, out=sys.stdout):
    out.write('┎─\n')
    for c in quote_stream_lines(process.stdout, prefix='┃ ', tmp_prefix='┋'):
        out.write(c)
        out.flush()
    out.write('\r┖─\n')


def VERBOSITY(v):
    return (logging.DEBUG + 1 - v) if v else logging.INFO


def partition(pred, xs):
    ts,fs = [],[]
    for x in xs:
        (ts if pred(x) else fs).append(x)
    return ts,fs


if __name__ == '__main__':
    exit(main())
