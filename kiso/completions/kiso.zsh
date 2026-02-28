#compdef kiso
# zsh completion for kiso

_kiso() {
    local -a commands instance_cmds skill_cmds connector_cmds env_cmds reset_cmds

    commands=(
        'instance:manage bot instances (create, start, stop, logs, ...)'
        'skill:manage skills'
        'connector:manage connectors'
        'sessions:list sessions'
        'env:manage deploy secrets'
        'msg:send a message and print the response'
        'reset:reset/cleanup data'
        'stats:show token usage statistics'
        'completion:print shell completion script'
        'version:print version and exit'
        'help:show help'
    )

    instance_cmds=(
        'create:create and start a new bot instance'
        'start:start a stopped instance'
        'stop:stop a running instance'
        'restart:restart an instance'
        'list:show all instances with ports and status'
        'status:container state + health check'
        'logs:follow container logs'
        'shell:open a bash shell inside the container'
        'explore:open a shell in the session workspace'
        'remove:remove instance and all its data'
    )

    skill_cmds=(
        'list:list installed skills'
        'search:search available skills'
        'install:install a skill'
        'update:update a skill'
        'remove:remove a skill'
    )

    connector_cmds=(
        'list:list installed connectors'
        'search:search available connectors'
        'install:install a connector'
        'update:update a connector'
        'remove:remove a connector'
        'run:start a connector daemon'
        'stop:stop a connector daemon'
        'status:show connector status'
    )

    env_cmds=(
        'set:set a deploy secret'
        'get:get a deploy secret'
        'list:list deploy secrets'
        'delete:delete a deploy secret'
        'reload:hot-reload secrets'
    )

    reset_cmds=(
        'session:reset one session'
        'knowledge:reset all knowledge'
        'all:reset all data'
        'factory:factory reset'
    )

    # Find the active top-level subcommand (skip --instance/-i and their values)
    local active_cmd="" i
    for (( i=2; i<=CURRENT-1; i++ )); do
        case "$words[i]" in
            --instance|-i) (( i++ )) ;;
            --instance=*) ;;
            -*) ;;
            *) active_cmd="$words[i]"; break ;;
        esac
    done

    case "$active_cmd" in
        instance)
            # Find instance sub-subcommand
            local sub_active="" j
            for (( j=i+1; j<=CURRENT-1; j++ )); do
                [[ "$words[j]" != -* ]] && { sub_active="$words[j]"; break; }
            done
            if [[ -z "$sub_active" ]]; then
                _describe -t instance-commands 'instance command' instance_cmds
            else
                case "$sub_active" in
                    remove)
                        _arguments '--yes[skip confirmation]' '-y[skip confirmation]'
                        compadd -- $(_kiso_instance_names)
                        ;;
                    start|stop|restart|status|logs|shell)
                        compadd -- $(_kiso_instance_names)
                        ;;
                    explore)
                        local -a session_names
                        session_names=(${(f)"$(_kiso_sessions)"})
                        (( ${#session_names} )) && compadd -- "${session_names[@]}"
                        ;;
                esac
            fi
            return
            ;;
        skill)
            if (( CURRENT == i+2 )); then
                _describe -t skill-commands 'skill command' skill_cmds
            elif (( CURRENT >= i+3 )); then
                case "$words[i+2]" in
                    install) _arguments '--name[skill name]:name' '--no-deps[skip dependencies]' ;;
                esac
            fi
            return
            ;;
        connector)
            if (( CURRENT == i+2 )); then
                _describe -t connector-commands 'connector command' connector_cmds
            elif (( CURRENT >= i+3 )); then
                case "$words[i+2]" in
                    install) _arguments '--name[connector name]:name' '--no-deps[skip dependencies]' ;;
                esac
            fi
            return
            ;;
        env)
            if (( CURRENT == i+2 )); then
                _describe -t env-commands 'env command' env_cmds
            fi
            return
            ;;
        reset)
            if (( CURRENT == i+2 )); then
                _describe -t reset-commands 'reset command' reset_cmds
            elif (( CURRENT >= i+3 )); then
                case "$words[i+2]" in
                    session)
                        local -a session_names
                        session_names=(${(f)"$(_kiso_sessions)"})
                        (( ${#session_names} )) && compadd -- "${session_names[@]}"
                        _arguments '--yes[skip confirmation]' '-y[skip confirmation]'
                        ;;
                    knowledge|all|factory)
                        _arguments '--yes[skip confirmation]' '-y[skip confirmation]'
                        ;;
                esac
            fi
            return
            ;;
        stats)
            _arguments \
                '--since[look back N days]:days' \
                '--session[filter by session]:session:->sessions' \
                '--by[group by dimension]:by:(model session role)' \
                '--all[show stats for all instances]'
            case "$state" in
                sessions)
                    local -a session_names
                    session_names=(${(f)"$(_kiso_sessions)"})
                    compadd -- "${session_names[@]}"
                    ;;
            esac
            return
            ;;
        sessions)
            if (( CURRENT == i+2 )); then
                local -a session_flags=('--all:show all sessions' '-a:show all sessions')
                _describe -t flags 'flag' session_flags
            fi
            return
            ;;
        completion)
            if (( CURRENT == i+2 )); then
                local -a shells=('bash:bash completion' 'zsh:zsh completion')
                _describe -t shells 'shell' shells
            fi
            return
            ;;
    esac

    # Top-level
    if [[ -z "$active_cmd" ]]; then
        if [[ "$words[CURRENT]" == -* ]]; then
            _arguments \
                '--instance[instance name]:name:->inst' \
                '-i[instance name]:name:->inst' \
                '--session[session name]:session:->sessions' \
                '--api[server URL]:url' \
                '--quiet[only show bot messages]' \
                '-q[only show bot messages]' \
                '--version[print version and exit]' \
                '-V[print version and exit]'
            case "$state" in
                inst)     compadd -- $(_kiso_instance_names) ;;
                sessions)
                    local -a snames
                    snames=(${(f)"$(_kiso_sessions)"})
                    compadd -- "${snames[@]}"
                    ;;
            esac
        else
            _describe -t commands 'kiso command' commands
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
    print('\n'.join(d.keys()))
" 2>/dev/null
}

# List sessions from the active instance DB.
# Detects --instance/-i NAME from $words (zsh completion global).
_kiso_sessions() {
    local inst=""
    local k
    for (( k=1; k<${#words}; k++ )); do
        case "$words[k]" in
            --instance|-i) inst="$words[k+1]"; break ;;
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
" 2>/dev/null)
    fi
    [[ -n "$inst" ]] && docker exec "kiso-$inst" sqlite3 /root/.kiso/kiso.db \
        "SELECT session FROM sessions" 2>/dev/null
}

_kiso "$@"
