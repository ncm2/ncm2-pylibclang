if get(s:, 'loaded', 0)
    finish
endif
let s:loaded = 1

let g:ncm2_pyclang#library_path = get(g:,
            \ 'ncm2_pyclang#library_path',
            \ '')

let g:ncm2_pyclang#clang_path = get(g:,
            \ 'ncm2_pyclang#clang_path',
            \ 'clang')

let g:ncm2_pyclang#database_path = get(g:,
            \ 'ncm2_pyclang#database_path',
            \ [
            \   'compile_commands.json',
            \   'build/compile_commands.json'
            \   ])

let g:ncm2_pyclang#args_file_path = get(g:,
            \ 'ncm2_pyclang#args_file_path',
            \ ['.clang_complete'])

let g:ncm2_pyclang#bin = get(g:, 'ncm2_pyclang#bin', "bin/ncm2_pyclang")

let g:ncm2_pyclang#source = extend(get(g:, 'ncm2_pyclang#source', {}), {
            \ 'name': 'pyclang',
            \ 'ready': 0,
            \ 'scope': ['cpp', 'c'],
            \ 'priority': 9,
            \ 'mark': 'cxx',
            \ 'subscope_enable': 1,
            \ 'on_complete': 'ncm2_pyclang#on_complete',
            \ 'on_warmup': 'ncm2_pyclang#on_warmup',
            \ 'complete_pattern': [
            \       '-\>',
            \       '::',
            \       '\.',
            \       '^\s*#',
            \       '^\s*#include.*/']
            \ }, 'keep')

let g:ncm2_pyclang#proc = yarp#py3({'module': 'ncm2_pyclang_proc',
            \ 'job_detach': 1,
            \ 'on_load': function('extend',
            \       [g:ncm2_pyclang#source, {'ready': 1}])
            \ })

func! ncm2_pyclang#init()
    call ncm2#register_source(g:ncm2_pyclang#source)
endfunc

func! ncm2_pyclang#on_warmup(ctx)
    if &filetype != 'cpp' && &filetype != 'c'
        call g:ncm2_pyclang#proc.jobstart()
        return
    endif
    if a:ctx['filepath'] == ""
        call g:ncm2_pyclang#proc.jobstart()
        return
    endif

    call g:ncm2_pyclang#proc.try_notify('cache_add',
                \ s:data(a:ctx),
                \ getline(1, '$'))

    if get(b:, 'b:ncm2_pyclang_cache') == 0
        au BufDelete <buffer> call 
                    \ g:ncm2_pyclang#proc.try_notify(
                    \   'cache_del',
                    \   expand('%:p'))
    endif

    let b:ncm2_pyclang_cache = 1
endfunc

func! ncm2_pyclang#on_complete(ctx)
    call g:ncm2_pyclang#proc.try_notify('on_complete',
                \ a:ctx,
                \ s:data({}),
                \ getline(1, '$'))
endfunc

func! ncm2_pyclang#find_declaration()
    let pos = g:ncm2_pyclang#proc.call('find_declaration',
                \ s:data(ncm2#context(g:ncm2_pyclang#source)),
                \ getline(1, '$'))
    if empty(pos)
        echohl ErrorMsg
        echom "Cannot find declaration"
        echohl None
    endif
    return pos
endfunc

func! ncm2_pyclang#goto_declaration()
    let pos = ncm2_pyclang#find_declaration()
    if empty(pos)
        return
    endif
    let filepath = expand("%:p")
    if filepath != pos.file
        let fes = fnameescape(pos.file)
        exe 'edit' fes
    else
        normal! m'
    endif
    call cursor(pos.lnum, pos.bcol)
endfunc

func! ncm2_pyclang#get_args_dir()
    return g:ncm2_pyclang#proc.call('get_args_dir',
                \ s:data(ncm2#context(g:ncm2_pyclang#source)))
endfunc

func! ncm2_pyclang#error(msg)
    call g:ncm2_pyclang#proc.error(a:msg)
endfunc

func! s:data(context)
    return  {'cwd': getcwd(),
                \ 'database_path': g:ncm2_pyclang#database_path,
                \ 'args_file_path': g:ncm2_pyclang#args_file_path,
                \ 'context': a:context,
                \ 'clang_path': g:ncm2_pyclang#clang_path,
                \ }
endfunc

