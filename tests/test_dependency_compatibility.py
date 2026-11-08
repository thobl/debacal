"""Dependency metadata avoids the known Qt binding crash on Python 3.11."""
from pathlib import Path
import tomllib

from packaging.requirements import Requirement
import pytest


@pytest.mark.parametrize('python_version', ['3.11', '3.12', '3.13', '3.14'])
def test_pyside_requirements_exclude_only_the_affected_python_release(python_version):
    project = tomllib.loads((Path(__file__).resolve().parents[1] / 'pyproject.toml').read_text())
    requirements = [Requirement(value) for value in project['project']['dependencies']]
    environment = {'python_version': python_version, 'python_full_version': python_version + '.0'}
    applicable = [requirement for requirement in requirements
                  if requirement.name.casefold() == 'pyside6'
                  and (requirement.marker is None or requirement.marker.evaluate(environment))]

    assert len(applicable) == 1
    supported = applicable[0].specifier
    assert '6.11.2' in supported
    assert '6.12.1' in supported
    assert ('6.12.0' in supported) == (python_version != '3.11')
