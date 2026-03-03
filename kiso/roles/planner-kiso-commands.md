Kiso management commands (use these in exec tasks when managing kiso itself):
- Skills: `kiso skill install <name>`, `kiso skill update <name|all>`, `kiso skill remove <name>`, `kiso skill list`, `kiso skill search [query]`
- Connectors: `kiso connector install <name>`, `kiso connector update <name|all>`, `kiso connector remove <name>`, `kiso connector run <name>`, `kiso connector stop <name>`, `kiso connector status <name>`, `kiso connector list`
- Env: `kiso env set KEY VALUE`, `kiso env get KEY`, `kiso env delete KEY`, `kiso env reload`
- Instance: `kiso instance status [name]`, `kiso instance restart [name]`, `kiso instance logs [name]`
- Users (admin only): `kiso user add <name> --role admin|user [--skills "*"|s1,s2] [--alias connector:id ...]`, `kiso user remove <name>`, `kiso user list`, `kiso user alias <name> --connector <conn> --id <id>`, `kiso user alias <name> --connector <conn> --remove`
