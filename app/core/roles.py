"""Workspace role defaults shared by every layer (no app imports, so no cycles)."""

# Role used when a case reaches this service without a valid one. The role is a
# mandatory field in the lawyer app and PETITIONER is pre-selected there, so a
# missing role is an upstream bug; falling back to the UI's own default keeps the
# panels on the side the lawyer would have seen, instead of a neutral judge's view.
DEFAULT_WORKSPACE_ROLE = "PETITIONER"
