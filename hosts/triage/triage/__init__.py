"""Triage: labels, de-duplicates and routes incoming GitHub issues for the Harborlight project.

Spec §5.9.3. Three model steps (``classify_type``, ``classify_component``, ``detect_duplicate``)
each run on the model that ``config/routing.yaml`` names; ``search_issues`` and ``get_issue``
read a frozen snapshot of historical issues; ``apply_label`` and ``comment`` are side-effecting
calls to a mock GitHub behind the chaos proxy.
"""
