# bash completion for kiso
# shellcheck shell=bash

_kiso() {
    local cur prev words cword
    _init_completion || return

    local commands="instance skill connector sessions env msg reset stats completion help version"
    local global_flags="--instance -i --session --api --quiet -q --user --help -h --version -V"

    local instance_cmds="create start stop restart list status logs shell explore remove"
    local skill_cmds="list search install update remove"
    local connector_cmds="list search install update remove run stop status"
    local env_cmds="set get list delete reload"
    local reset_cmds="session knowledge all factory"

    # Find the active top-level subcommand (skip --instance/-i and their values)
    local i active_cmd=""
    for (( i=1; i<cword; i++ )); do
        case "${words[i]}" in
            --instance|-i) (( i++ )) ;;
            --instance=*) ;;
            -*) ;;
            *) active_cmd="${words[i]}"; break ;;
        esac
    done

    case "$active_cmd" in
        instance)
            # Find instance sub-subcommand
            local j sub_active=""
            for (( j=i+1; j<cword; j++ )); do
                [[ "${words[j]}" != -* ]] && { sub_active="${words[j]}"; break; }
            done
            case "$sub_active" in
                "")
                    COMPREPLY=($(compgen -W "$instance_cmds" -- "$cur"))
                    ;;
                remove)
                    COMPREPLY=($(compgen -W "--yes -y $(_kiso_instance_names)" -- "$cur"))
                    ;;
                start|stop|restart|status|logs|shell)
                    COMPREPLY=($(compgen -W "$(_kiso_instance_names)" -- "$cur"))
                    ;;
                explore)
                    COMPREPLY=($(compgen -W "$(_kiso_sessions)" -- "$cur"))
                    ;;
            esac
            return
            ;;
        skill)
            local skill_pos=$(( cword - i - 1 ))
            if (( skill_pos == 1 )); then
                COMPREPLY=($(compgen -W "$skill_cmds" -- "$cur"))
            elif (( skill_pos >= 2 )); then
                local skill_sub="${words[i+1]}"
                case "$skill_sub" in
                    install) COMPREPLY=($(compgen -W "--name --no-deps" -- "$cur")) ;;
                esac
            fi
            return
            ;;
        connector)
            local conn_pos=$(( cword - i - 1 ))
            if (( conn_pos == 1 )); then
                COMPREPLY=($(compgen -W "$connector_cmds" -- "$cur"))
            elif (( conn_pos >= 2 )); then
                local conn_sub="${words[i+1]}"
                case "$conn_sub" in
                    install) COMPREPLY=($(compgen -W "--name --no-deps" -- "$cur")) ;;
                esac
            fi
            return
            ;;
        env)
            local env_pos=$(( cword - i - 1 ))
            if (( env_pos == 1 )); then
                COMPREPLY=($(compgen -W "$env_cmds" -- "$cur"))
            fi
            return
            ;;
        reset)
            local reset_pos=$(( cword - i - 1 ))
            if (( reset_pos == 1 )); then
                COMPREPLY=($(compgen -W "$reset_cmds" -- "$cur"))
            elif (( reset_pos >= 2 )); then
                local reset_sub="${words[i+1]}"
                case "$reset_sub" in
                    session)
                        local sessions
                        sessions=$(_kiso_sessions)
                        COMPREPLY=($(compgen -W "$sessions --yes -y" -- "$cur"))
                        ;;
                    knowledge|all|factory)
                        COMPREPLY=($(compgen -W "--yes -y" -- "$cur"))
                        ;;
                esac
            fi
            return
            ;;
        stats)
            case "$prev" in
                --since) return ;;
                --session)
                    COMPREPLY=($(compgen -W "$(_kiso_sessions)" -- "$cur"))
                    ;;
                --by)
                    COMPREPLY=($(compgen -W "model session role" -- "$cur"))
                    ;;
                *)
                    COMPREPLY=($(compgen -W "--since --session --by --all" -- "$cur"))
                    ;;
            esac
            return
            ;;
        sessions)
            COMPREPLY=($(compgen -W "--all -a" -- "$cur"))
            return
            ;;
        completion)
            local comp_pos=$(( cword - i - 1 ))
            if (( comp_pos == 1 )); then
                COMPREPLY=($(compgen -W "bash zsh" -- "$cur"))
            fi
            return
            ;;
    esac

    # Top-level: complete commands and global flags
    if [[ -z "$active_cmd" ]]; then
        if [[ "$cur" == -* ]]; then
            COMPREPLY=($(compgen -W "$global_flags" -- "$cur"))
        else
            COMPREPLY=($(compgen -W "$commands" -- "$cur"))
        fi
    fi
}

# List instance names from ~/.kiso/instances.json
_kiso_instance_names() {
    python3 -c "
import json, pathlib, os
p = pathlib.Path(os.path.expanduser('~/.kiso/instances.json'))
if p.exists():
    d = json.loads(p.read_text())
    print(' '.join(d.keys()))
" 2>/dev/null || true
}

# List sessions from the active instance DB.
# Detects --instance/-i NAME from the current command line (COMP_WORDS is a
# bash completion global available in helper functions during completion).
_kiso_sessions() {
    local inst=""
    local k
    for (( k=1; k<${#COMP_WORDS[@]}-1; k++ )); do
        case "${COMP_WORDS[k]}" in
            --instance|-i) inst="${COMP_WORDS[k+1]}"; break ;;
        esac
    done
    if [[ -z "$inst" ]]; then
        inst=$(python3 -c "
import json, pathlib, os
p = pathlib.Path(os.path.expanduser('~/.kiso/instances.json'))
if p.exists():
    d = json.loads(p.read_text())
    if len(d) == 1:
        print(list(d.keys())[0])
" 2>/dev/null || true)
    fi
    [[ -n "$inst" ]] && docker exec "kiso-$inst" sqlite3 /root/.kiso/kiso.db \
        "SELECT session FROM sessions" 2>/dev/null || true
}

complete -F _kiso kiso
