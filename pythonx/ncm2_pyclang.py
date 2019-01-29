from ncm2 import getLogger
from os.path import dirname, join, isfile, normpath, expanduser, expandvars
from pathlib import Path
import shlex
import json

logger = getLogger(__name__)


def _extract_args_from_cmake(cmd):
    args = None
    if 'command' in cmd:
        # the last arg is filename
        args = shlex.split(cmd['command'])[:-1]
    elif 'arguments' in cmd:
        # the last arg is filename
        args = cmd['arguments'][:-1]
    
    # filter for ccache
    while args and not args[0].startswith("-"):
        args = args[1:]
    return args

def args_from_cmake(filepath, cwd, database_paths):
    filedir = dirname(filepath)

    cfg_path, _ = find_config([filedir, cwd], database_paths)
    resolve = lambda fp: normpath(join(dirname(cfg_path), fp))

    if not cfg_path:
        return None, None

    filepath = normpath(filepath)

    try:
        with open(cfg_path, "r") as f:
            commands = json.load(f)

            for cmd in commands:
                try:
                    cmd_for = join(cmd['directory'], cmd['file'])
                    if normpath(cmd_for) == filepath:
                        logger.info("compile_commands: %s", cmd)
                        args = _extract_args_from_cmake(cmd)

                        fixed_args = []
                        add_next = False
                        for arg in args:
                            if add_next:
                                add_next = False
                                fixed_args.append('-I' + resolve(arg))
                            elif arg == '-I':
                                add_next = True
                            elif arg.startswith('-I'):
                                fixed_args.append('-I' + resolve(arg[2:]))
                            else:
                                fixed_args.append(arg)

                        return fixed_args, cmd['directory']
                except Exception as ex:
                    logger.exception("Exception processing %s", cmd)

            logger.error("Failed finding args from %s for %s", cfg_path, filepath)

            # Merge all include dirs and the flags of the last item as a
            # fallback. This is useful for editting header file.
            all_dirs = {}
            for cmd in commands:
                args = _extract_args_from_cmake(cmd)
                add_next = False
                for arg in args:
                    if add_next:
                        add_next = False
                        all_dirs['-I' + resolve(arg)] = True
                    elif arg == "-I":
                        add_next = True
                    elif arg.startswith("-I"):
                        all_dirs['-I' + resolve(arg[2:])] = True

            return list(all_dirs.keys()) + args, filedir

    except Exception as ex:
        logger.exception("read compile_commands.json [%s] failed.", cfg_path)

    return None, None


def args_from_clang_complete(filepath, cwd, args_file_path):
    filedir = dirname(filepath)

    clang_complete, directory = find_config([filedir, cwd], args_file_path)

    if not clang_complete:
        return None, None

    try:
        with open(clang_complete, "r") as f:
            args = shlex.split(" ".join(f.readlines()))
            args = [expanduser(expandvars(p)) for p in args]
            logger.info('.clang_complete args: %s', args)
            return args, directory
    except Exception as ex:
        logger.exception("read config file %s failed.", clang_complete)

    return None, None


def find_config(bases, names):
    if isinstance(names, str):
        names = [names]

    if isinstance(bases, str):
        bases = [bases]

    for base in bases:
        r = Path(base).resolve()
        dirs = [r] + list(r.parents)
        for d in dirs:
            d = str(d)
            for name in names:
                p = join(d, name)
                if isfile(p):
                    return p, d

    return None, None
