"""
bootstrap/__init__.py — AgentOS Bootstrap Package

Provides the core provisioning library for applying an agentOS.yaml spec
to a target GitHub repository.

Public API:
  from bootstrap import load_spec, BootstrapState
  from bootstrap.labels import sync_labels
  from bootstrap.board import provision_board
  from bootstrap.apps import register_apps
  from bootstrap.workflows import copy_workflows
  from bootstrap.verify import verify_repo
  from bootstrap.apply import apply_all
"""

__version__ = "1.0.0"
