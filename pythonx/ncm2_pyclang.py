from ncm2 import getLogger
from os.path import dirname, join, isfile, isdir, samefile, expanduser, expandvars
from pathlib import Path
import os
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

    cfg_path = find_config([filedir, cwd], database_paths)

    if not cfg_path:
        return None, None

    try:
        with open(cfg_path, "r") as f:
            commands = json.load(f)

            for cmd in commands:
                try:
                    if samefile(join(cmd['directory'], cmd['file']), filepath):
                        logger.info("compile_commands: %s", cmd)
                        args = _extract_args_from_cmake(cmd)
                        return args, cmd['directory']
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
                        all_dirs['-I' + arg] = True
                    if arg == "-I":
                        add_next = True
                        continue
                    if arg.startswith("-I"):
                        all_dirs['-I' + arg[2:]] = True

            return list(all_dirs.keys()) + args, filedir

    except Exception as ex:
        logger.exception("read compile_commands.json [%s] failed.", cfg_path)

    return None, None


def args_from_clang_complete(filepath, cwd):
    filedir = dirname(filepath)

    clang_complete, run_dir = find_config([filedir, cwd], '.clang_complete')

    if not clang_complete:
        return None, None

    try:
        with open(clang_complete, "r") as f:
            args = shlex.split(" ".join(f.readlines()))
            args = [expanduser(expandvars(p)) for p in args]
            logger.info('.clang_complete args: %s', args)
            return args, run_dir
    except Exception as ex:
        logger.exception("read config file %s failed.", clang_complete)

    return None, None

def _find_scm_dir(scm_names):
    cwd = Path(os.getcwd())
    dirs = [cwd.resolve()] + list(cwd.parents)
    for d in dirs:
        for name in scm_names:
            scm_dir = join(str(d), name)
            if isdir(scm_dir):
                return scm_dir
    return ''

def find_config(bases, names):
    if isinstance(names, str):
        names = [names]

    if isinstance(bases, str):
        bases = [bases]

    scm_dir = _find_scm_dir(['.git', '.svn', '.hg'])
    if scm_dir:
        for name in names:
            p = join(scm_dir, name)
            if isfile(p):
                return p, str(Path(scm_dir).parent)

    for base in bases:
        r = Path(base).resolve()
        dirs = [r] + list(r.parents)
        for d in dirs:
            d = str(d)
            for name in names:
                p = join(d, name)
                if isfile(p):
                    return p, dirname(p)

    return None, None
