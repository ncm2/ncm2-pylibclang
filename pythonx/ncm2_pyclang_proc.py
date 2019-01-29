# -*- coding: utf-8 -*-

from ncm2 import Ncm2Source, getLogger, Popen
import subprocess
import re
from os.path import dirname
from os import path, scandir
import vim
import json
import shlex
import time
import threading
import queue
import traceback

import sys
sys.path.insert(0, path.join(dirname(__file__), '3rd'))

from ncm2_pyclang import args_from_cmake, args_from_clang_complete
from clang import cindex
from clang.cindex import CodeCompletionResult, CompletionString, SourceLocation, Cursor, File, Diagnostic

logger = getLogger(__name__)


class ErrTaskCancel(Exception):
    pass


class Source(Ncm2Source):

    def __init__(self, nvim):
        Ncm2Source.__init__(self, nvim)

        library_path = nvim.vars['ncm2_pyclang#library_path']
        if path.isdir(library_path):
            cindex.Config.set_library_path(library_path)
        elif path.isfile(library_path):
            cindex.Config.set_library_file(library_path)

        cindex.Config.set_compatibility_check(False)

        self.cmpl_index = cindex.Index.create(excludeDecls=False)
        self.goto_index = cindex.Index.create(excludeDecls=False)

        self.cmpl_tu = {}
        self.goto_tu = {}

        self.queue = queue.Queue()
        self.worker = threading.Thread(target=self.worker_loop)
        self.worker.daemon = True
        self.worker.start()

    def join_queue(self):
        if self.worker.is_alive() and \
                self.worker is not threading.current_thread():
            self.queue.join()

    def worker_loop(self):
        while True:
            name, task = self.queue.get()
            if task is None:
                break
            logger.info('begin task %s', name)
            try:
                task()
                logger.info('task %s finished', name)
            except ErrTaskCancel as ex:
                logger.info('task %s canceled, %s', name, ex)
            except Exception as ex:
                traceback.print_exc()
                logger.exception('exception: %s', ex)
            finally:
                self.queue.task_done()

    def notify(self, method: str, *args):
        self.nvim.call(method, *args, async_=True)

    def get_args_dir(self, data):
        self.join_queue()
        context = data['context']
        cwd = data['cwd']
        database_path = data['database_path']
        filepath = context['filepath']
        args_file_path = data['args_file_path']

        args = []

        run_dir = cwd
        cmake_args, directory = args_from_cmake(filepath, cwd, database_path)
        if cmake_args is not None:
            args = cmake_args
            run_dir = directory
        else:
            clang_complete_args, directory = args_from_clang_complete(
                filepath, cwd, args_file_path)
            if clang_complete_args:
                args = clang_complete_args
                run_dir = directory

        if context['scope'] == 'cpp':
            args.append('-xc++')
        elif context['filetype'] == 'cpp':
            args.append('-xc++')
        else:
            args.append('-xc')

        return [args, run_dir]

    def cache_add(self, data, lines):
        self.join_queue()
        self.do_cache_add(data, lines, True)
        self.do_cache_add(data, lines, False)

    def do_cache_add(self, data, lines, for_completion):
        context = data['context']
        src = self.get_src("\n".join(lines), context)
        filepath = context['filepath']
        changedtick = context['changedtick']
        args, directory = self.get_args_dir(data)
        start = time.time()

        if for_completion:
            cache = self.cmpl_tu
        else:
            cache = self.goto_tu

        check = dict(args=args, directory=directory)
        if filepath in cache:
            item = cache[filepath]
            if check == item['check']:
                tu = item['tu']
                if changedtick == item['changedtick']:
                    logger.info("changedtick is the same, skip reparse")
                    return
                self.reparse_tu(tu, filepath, src)
                logger.debug("cache_add reparse existing done")
                return
            del cache[filepath]

        item = {}
        item['check'] = check
        item['changedtick'] = changedtick

        tu = self.create_tu(filepath, args, directory, src,
                            for_completion=for_completion)
        item['tu'] = tu

        cache[filepath] = item

        end = time.time()
        logger.debug("cache_add done cmpl[%s]. time: %s",
                     for_completion,
                     end - start)

    def cache_del(self, filepath):
        self.join_queue()
        if filepath in self.cmpl_tu:
            del self.cmpl_tu[filepath]
            logger.info('completion cache %s has been removed', filepath)
        if filepath in self.goto_tu:
            del self.goto_tu[filepath]
            logger.info('goto cache %s has been removed', filepath)

    def get_tu(self, filepath, args, directory, src, for_completion=False):
        if for_completion:
            cache = self.cmpl_tu
        else:
            cache = self.goto_tu

        check = dict(args=args, directory=directory)
        if filepath in cache:
            item = cache[filepath]
            tu = item['tu']
            if check == item['check']:
                logger.info("%s tu is cached", filepath)
                self.reparse_tu(tu, filepath, src)
                return item['tu']
            logger.info("%s tu invalidated by check %s -> %s",
                        filepath, check, item['check'])
            self.cache_del(filepath)

        logger.info("cache miss")

        return self.create_tu(filepath,
                              args,
                              directory,
                              src,
                              for_completion=for_completion)

    def args_to_clang_cc1(self, data, args, directory):
        # Translate to clang args
        # clang-5.0 -### -x c++  -c -
        cmd = [data['clang_path'], '-###'] + args + ['-']
        logger.debug('to clang cc1 cmd: %s', cmd)

        proc = Popen(args=cmd,
                     stdin=subprocess.PIPE,
                     stdout=subprocess.PIPE,
                     stderr=subprocess.PIPE)

        outdata, errdata = proc.communicate('', timeout=2)
        logger.debug('outdata: %s, errdata: %s', outdata, errdata)
        if proc.returncode != 0:
            return None

        errdata = errdata.decode()

        lines = errdata.splitlines()
        installed_dir_found = False
        for line in lines:
            if not installed_dir_found:
                if line.startswith('InstalledDir:'):
                    installed_dir_found = True
                continue
            args = shlex.split(line)
            # remove clang binary and the last '-', insert working directory
            # after -cc1
            args = args[1:-1]
            args.insert(1, '-working-directory=' + directory)
            logger.debug('-cc1 args: %s', args)
            return args

        return None

    def create_tu(self, filepath, args, directory, src, for_completion):

        CXTranslationUnit_KeepGoing = 0x200
        CXTranslationUnit_CreatePreambleOnFirstParse = 0x100

        args = ['-working-directory='] + args

        if not for_completion:
            flags = cindex.TranslationUnit.PARSE_PRECOMPILED_PREAMBLE | \
                cindex.TranslationUnit.PARSE_INCOMPLETE | \
                CXTranslationUnit_CreatePreambleOnFirstParse | \
                cindex.TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD | \
                CXTranslationUnit_KeepGoing
        else:
            flags = cindex.TranslationUnit.PARSE_PRECOMPILED_PREAMBLE | \
                cindex.TranslationUnit.PARSE_INCOMPLETE | \
                CXTranslationUnit_CreatePreambleOnFirstParse | \
                cindex.TranslationUnit.PARSE_CACHE_COMPLETION_RESULTS | \
                cindex.TranslationUnit.PARSE_SKIP_FUNCTION_BODIES | \
                CXTranslationUnit_KeepGoing

        logger.info("flags %s", flags)

        unsaved = (filepath, src)

        if for_completion:
            index = self.cmpl_index
        else:
            index = self.goto_index

        tu = index.parse(filepath, args, [unsaved], flags)
        return tu

    def reparse_tu(self, tu, filepath, src):
        unsaved = (filepath, src)
        tu.reparse([unsaved])

    include_pat = re.compile(r'^\s*#include\s+["<]([^"<]*)$')
    include_base_pat = re.compile(r'([^/"<]*)$')

    def get_include_completions(self, data, args, directory, inc_typed):
        context = data['context']
        cc1 = self.args_to_clang_cc1(data, args, directory)
        if cc1:
            args = cc1

        includes = []
        next_is_include = False
        opts = ['-I', '-isystem', '-internal-isystem',
                '-internal-externc-isystem']
        for arg in args:
            if not next_is_include:
                if arg in opts:
                    next_is_include = True
                    continue
                for opt in opts:
                    if arg.startswith(opt):
                        start = len(opt)
                        if start > 2:
                            start += 1
                        includes.append(arg[start:])
                        break
                continue
            includes.append(arg)
            next_is_include = False

        includes = [path.normpath(path.join(directory, inc))
                    for inc in includes]

        # current file path
        if context['filepath']:
            includes.append(dirname(context['filepath']))

        # remove duplicate
        includes = list(set(includes))

        logger.debug("includes to search: %s", includes)

        matches = []
        matcher = self.matcher_get(context['matcher'])

        sub_dir = dirname(inc_typed)  # type: str
        sub_dir = sub_dir.strip('/')
        base = self.include_base_pat.search(inc_typed).group(1)

        for inc in includes:
            try:
                for entry in scandir(path.join(inc, sub_dir)):
                    name = entry.name
                    match = self.match_formalize(context, name)
                    match['menu'] = path.join(inc, sub_dir, name)
                    if entry.is_dir():
                        match['menu'] += '/'
                    if not matcher(base, match):
                        continue
                    matches.append(match)
            except:
                logger.exception('scandir failed for %s', inc)

        startccol = context['ccol'] - len(base)

        cb = lambda: self.complete(context, startccol, matches)
        self.nvim.async_call(cb)

    def on_complete(self, context, data, lines):
        self.on_complete_context_id = context['context_id']
        self.queue.put(['on_complete',
                        lambda: self.on_complete_task(context, data, lines)])

    def on_complete_task(self, context, data, lines):
        context_id = context['context_id']

        def check_context_id(info, *args):
            if context_id != self.on_complete_context_id:
                raise ErrTaskCancel(info % (*args,))

        data['context'] = context
        src = self.get_src("\n".join(lines), context)
        filepath = context['filepath']
        startccol = context['startccol']
        bcol = context['bcol']
        lnum = context['lnum']
        base = context['base']
        typed = context['typed']

        check_context_id('get_args_dir')

        args, directory = self.get_args_dir(data)

        inc_match = self.include_pat.search(typed)
        if inc_match:
            self.get_include_completions(data,
                                         args,
                                         directory,
                                         inc_match.group(1))
            return

        start = time.time()

        check_context_id('get_tu')

        tu = self.get_tu(filepath, args, directory, src)

        check_context_id('codeComplete')

        unsaved = [filepath, src]
        cr = tu.codeComplete(filepath,
                             lnum,
                             bcol,
                             [unsaved],
                             include_macros=True,
                             include_code_patterns=True)
        results = cr.results

        cr_end = time.time()

        matcher = self.matcher_get(context['matcher'])

        matches = []
        for i, res in enumerate(results):
            now = time.time()
            check_context_id('complete result %s/%s', i + 1, len(results))
            item = self.format_complete_item(context, matcher, base, res)
            if item is None:
                continue
            # filter it's kind of useless for completion
            if item['word'].startswith('operator '):
                continue
            item = self.match_formalize(context, item)
            if not matcher(base, item):
                continue
            matches.append(item)

        end = time.time()
        logger.debug("total time: %s, codeComplete time: %s, matches %s -> %s",
                     end - start, cr_end - start, len(results), len(matches))

        cb = lambda: self.complete(context, startccol, matches)
        self.nvim.async_call(cb)

    def format_complete_item(self, context, matcher, base, result):
        result_type = None
        word = ''
        snippet = ''
        info = ''

        def roll_out_optional(chunks: CompletionString):
            result = []
            word = ""
            for chunk in chunks:
                if chunk.isKindInformative():
                    continue
                if chunk.isKindResultType():
                    continue
                if chunk.isKindTypedText():
                    continue
                word += chunk.spelling
                if chunk.isKindOptional():
                    result += roll_out_optional(chunk.string)
            return [word] + result

        placeholder_num = 1

        for chunk in result.string:

            if chunk.isKindTypedText():
                # filter the matches earlier for performance
                tmp = self.match_formalize(context, chunk.spelling)
                if not matcher(base, tmp):
                    return None
                word = chunk.spelling

            if chunk.isKindInformative():
                continue

            if chunk.isKindResultType():
                result_type = chunk
                continue

            chunk_text = chunk.spelling

            if chunk.isKindOptional():
                for arg in roll_out_optional(chunk.string):
                    snippet += self.lsp_snippet_placeholder(
                        placeholder_num, arg)
                    placeholder_num += 1
                    info += "[" + arg + "]"
            elif chunk.isKindPlaceHolder():
                snippet += self.lsp_snippet_placeholder(
                    placeholder_num, chunk_text)
                placeholder_num += 1
                info += chunk_text
            else:
                snippet += chunk_text
                info += chunk_text

        menu = info

        if result_type:
            result_text = result_type.spelling
            menu = result_text + " " + menu

        completion = dict()
        completion['word'] = word
        ud = {}
        if snippet != word:
            ud['is_snippet'] = 1
            ud['snippet'] = snippet
        completion['user_data'] = ud
        completion['menu'] = menu
        completion['info'] = info
        completion['dup'] = 1
        return completion

    def lsp_snippet_placeholder(self, num, txt=''):
        txt = txt.replace('\\', '\\\\')
        txt = txt.replace('$', r'\$')
        txt = txt.replace('}', r'\}')
        if txt == '':
            return '${%s}' % num
        return '${%s:%s}' % (num, txt)

    def find_declaration(self, data, lines):
        self.join_queue()

        context = data['context']
        src = self.get_src("\n".join(lines), context)
        filepath = context['filepath']
        bcol = context['bcol']
        lnum = context['lnum']

        args, directory = self.get_args_dir(data)

        tu = self.get_tu(filepath, args, directory, src)

        f = File.from_name(tu, filepath)
        location = SourceLocation.from_position(tu, f, lnum, bcol)
        cursor = Cursor.from_location(tu, location)

        defs = [cursor.get_definition(), cursor.referenced]
        for d in defs:
            if d is None:
                logger.info("d None")
                continue

            d_loc = d.location
            if d_loc.file is None:
                logger.info("location.file None")
                continue

            ret = {}
            ret['file'] = d_loc.file.name
            ret['lnum'] = d_loc.line
            ret['bcol'] = d_loc.column
            return ret

        # we failed finding the declaration, maybe there's some syntax error
        # stopping us. Report it to the user.
        logger.info('reading Diagnostic for this tu, args: %s', args)
        for diag in tu.diagnostics:
            # type: Diagnostic
            if diag.severity < diag.Error:
                pass
            self.nvim.call('ncm2_pyclang#error', diag.format())
        return {}


source = Source(vim)

on_complete = source.on_complete
cache_add = source.cache_add
find_declaration = source.find_declaration
cache_del = source.cache_del
get_args_dir = source.get_args_dir
