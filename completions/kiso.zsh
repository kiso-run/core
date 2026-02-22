#compdef kiso
# zsh completion for kiso

_kiso() {
    local -a commands skill_cmds connector_cmds env_cmds

    commands=(
        'up:start the container'
        'down:stop the container'
        'restart:restart the container'
        'shell:open a bash shell inside the container'
        'explore:open a shell in the session workspace'
        'logs:follow container logs'
        'status:show container state and health'
        'health:hit the /health endpoint'
        'help:show help'
        'skill:manage skills'
        'connector:manage connectors'
        'sessions:manage sessions'
        'env:manage deploy secrets'
        'completion:print shell completion script'
        'msg:send a message and print the response'
        'reset:reset/cleanup data'
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
        'run:run a connector'
        'stop:stop a connector'
        'status:show connector status'
    )

    env_cmds=(
        'set:set a deploy secret'
        'get:get a deploy secret'
        'list:list deploy secrets'
        'delete:delete a deploy secret'
        'reload:hot-reload secrets'
    )

    # Handle subcommands
    case "$words[2]" in
        skill)
            if (( CURRENT == 3 )); then
                _describe -t skill-commands 'skill command' skill_cmds
            elif (( CURRENT >= 4 )); then
                case "$words[3]" in
                    install) _arguments '--name[skill name]:name' '--no-deps[skip dependencies]' ;;
                esac
            fi
            return
            ;;
        connector)
            if (( CURRENT == 3 )); then
                _describe -t connector-commands 'connector command' connector_cmds
            elif (( CURRENT >= 4 )); then
                case "$words[3]" in
                    install) _arguments '--name[connector name]:name' '--no-deps[skip dependencies]' ;;
                esac
            fi
            return
            ;;
        env)
            if (( CURRENT == 3 )); then
                _describe -t env-commands 'env command' env_cmds
            fi
            return
            ;;
        reset)
            if (( CURRENT == 3 )); then
                local -a reset_cmds=(
                    'session:reset one session'
                    'knowledge:reset all knowledge'
                    'all:reset all data'
                    'factory:factory reset'
                )
                _describe -t reset-commands 'reset command' reset_cmds
            elif (( CURRENT >= 4 )); then
                case "$words[3]" in
                    session)
                        # Dynamic session name completion from DB
                        local -a session_names
                        session_names=(${(f)"$(docker exec kiso sqlite3 /root/.kiso/store.db 'SELECT session FROM sessions' 2>/dev/null)"})
                        if (( ${#session_names} )); then
                            compadd -- "${session_names[@]}"
                        fi
                        _arguments '--yes[skip confirmation]' '-y[skip confirmation]'
                        ;;
                    knowledge|all|factory)
                        _arguments '--yes[skip confirmation]' '-y[skip confirmation]'
                        ;;
                esac
            fi
            return
            ;;
        sessions)
            if (( CURRENT == 3 )); then
                local -a session_flags=('--all:show all sessions' '-a:show all sessions')
                _describe -t flags 'flag' session_flags
            fi
            return
            ;;
        completion)
            if (( CURRENT == 3 )); then
                local -a shells=('bash:bash completion' 'zsh:zsh completion')
                _describe -t shells 'shell' shells
            fi
            return
            ;;
        up|down|restart|shell|explore|logs|status|health|help|msg)
            return
            ;;
    esac

    # Top-level completion
    if (( CURRENT == 2 )); then
        _describe -t commands 'kiso command' commands
    fi
}

_kiso "$@"
