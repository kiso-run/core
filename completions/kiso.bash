# bash completion for kiso
# shellcheck shell=bash

_kiso() {
    local cur prev words cword
    _init_completion || return

    local commands="up down restart shell explore logs status health help skill connector sessions env completion msg reset"
    local global_flags="--session --api --quiet -q --user --help -h"

    local skill_cmds="list search install update remove"
    local connector_cmds="list search install update remove run stop status"
    local env_cmds="set get list delete reload"
    local reset_cmds="session knowledge all factory"

    # Determine which top-level command was given
    case "${words[1]}" in
        skill)
            case "$cword" in
                2) COMPREPLY=($(compgen -W "$skill_cmds" -- "$cur")) ;;
                *)
                    case "${words[2]}" in
                        install) COMPREPLY=($(compgen -W "--name --no-deps" -- "$cur")) ;;
                    esac
                    ;;
            esac
            return
            ;;
        connector)
            case "$cword" in
                2) COMPREPLY=($(compgen -W "$connector_cmds" -- "$cur")) ;;
                *)
                    case "${words[2]}" in
                        install) COMPREPLY=($(compgen -W "--name --no-deps" -- "$cur")) ;;
                    esac
                    ;;
            esac
            return
            ;;
        env)
            if (( cword == 2 )); then
                COMPREPLY=($(compgen -W "$env_cmds" -- "$cur"))
            fi
            return
            ;;
        reset)
            case "$cword" in
                2) COMPREPLY=($(compgen -W "$reset_cmds" -- "$cur")) ;;
                *)
                    case "${words[2]}" in
                        session)
                            if [[ "$cur" == -* ]]; then
                                COMPREPLY=($(compgen -W "--yes -y" -- "$cur"))
                            else
                                # Dynamic session name completion from DB
                                local sessions
                                sessions=$(docker exec kiso sqlite3 /root/.kiso/store.db "SELECT session FROM sessions" 2>/dev/null)
                                COMPREPLY=($(compgen -W "$sessions --yes -y" -- "$cur"))
                            fi
                            ;;
                        knowledge|all|factory)
                            COMPREPLY=($(compgen -W "--yes -y" -- "$cur"))
                            ;;
                    esac
                    ;;
            esac
            return
            ;;
        sessions)
            COMPREPLY=($(compgen -W "--all -a" -- "$cur"))
            return
            ;;
        completion)
            if (( cword == 2 )); then
                COMPREPLY=($(compgen -W "bash zsh" -- "$cur"))
            fi
            return
            ;;
        up|down|restart|shell|explore|logs|status|health|help|msg)
            return
            ;;
    esac

    # Top-level: complete commands and global flags
    if (( cword == 1 )); then
        COMPREPLY=($(compgen -W "$commands $global_flags" -- "$cur"))
    fi
}

complete -F _kiso kiso
