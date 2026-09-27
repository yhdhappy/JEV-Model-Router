# Expected paths

- README.md

# Forbidden paths

None. No path may be modified; this is encoded by `acceptance.allowed_paths: []`
in `task.yaml`, because the current runner uses `forbidden_paths` for paths that
must not exist, not for detecting changes.
