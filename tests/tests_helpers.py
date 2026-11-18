def good_manifest(**over):
    m = {"schema": 1, "manifest_version": 2, "latest_version": None, "min_version": None,
         "disabled_tools": [], "tool_notes": {}, "tools": []}
    m.update(over)
    return m
