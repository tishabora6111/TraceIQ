"""Service-level guarantees: transactions, rollback, and role re-checks under the project lock."""

import uuid
from collections.abc import Callable

import pytest
from sqlalchemy import func, select

from app.core.database import get_session_factory
from app.modules.projects import repository, service
from app.modules.projects.errors import (
    DeletionConfirmationError,
    InsufficientProjectRoleError,
    ProjectNotFoundError,
)
from app.modules.projects.models import Project, ProjectMember, ProjectRole
from app.modules.projects.schemas import ProjectAccess
from tests.projects.conftest import Caller

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("clean_database")]

MakeCaller = Callable[[str], Caller]


def _boom(*args: object, **kwargs: object) -> None:
    raise RuntimeError("injected failure")


def _count(model: type) -> int:
    with get_session_factory()() as session:
        return session.scalar(select(func.count()).select_from(model)) or 0


def _create(owner: Caller, name: str = "Project") -> uuid.UUID:
    with get_session_factory()() as session:
        return service.create_project(session, user_id=owner.user.id, name=name, description=None).id


def _set_role(project_id: uuid.UUID, user_id: uuid.UUID, role: ProjectRole) -> None:
    with get_session_factory()() as session:
        member = session.get_one(ProjectMember, (project_id, user_id))
        member.role = role.value
        session.commit()


def test_role_order() -> None:
    assert service.role_allows(ProjectRole.OWNER, ProjectRole.ADMIN)
    assert service.role_allows(ProjectRole.ADMIN, ProjectRole.ADMIN)
    assert service.role_allows(ProjectRole.MEMBER, ProjectRole.MEMBER)
    assert not service.role_allows(ProjectRole.MEMBER, ProjectRole.ADMIN)
    assert not service.role_allows(ProjectRole.ADMIN, ProjectRole.OWNER)


def test_create_is_atomic(make_caller: MakeCaller, monkeypatch: pytest.MonkeyPatch) -> None:
    """If the Owner membership cannot be written, no project is left behind."""
    owner = make_caller("owner@example.com")
    monkeypatch.setattr(repository, "insert_member", _boom)

    with get_session_factory()() as session:
        with pytest.raises(RuntimeError, match="injected failure"):
            service.create_project(session, user_id=owner.user.id, name="Project", description=None)
        assert not session.in_transaction()

    assert _count(Project) == 0


def test_create_rejects_an_unknown_user_and_rolls_back() -> None:
    with get_session_factory()() as session:
        with pytest.raises(Exception, match="fk_project_members_user_id_users"):
            service.create_project(session, user_id=uuid.uuid4(), name="Project", description=None)
        assert not session.in_transaction()
    assert _count(Project) == 0


def test_get_project_access(make_caller: MakeCaller) -> None:
    owner, stranger = make_caller("owner@example.com"), make_caller("stranger@example.com")
    project_id = _create(owner)

    with get_session_factory()() as session:
        access = service.get_project_access(
            session, project_id=project_id, user_id=owner.user.id, minimum=ProjectRole.OWNER
        )
        assert access == ProjectAccess(project_id=project_id, user_id=owner.user.id, role=ProjectRole.OWNER)
        with pytest.raises(ProjectNotFoundError):
            service.get_project_access(session, project_id=project_id, user_id=stranger.user.id, minimum=ProjectRole.MEMBER)
        with pytest.raises(ProjectNotFoundError):
            service.get_project_access(session, project_id=uuid.uuid4(), user_id=owner.user.id, minimum=ProjectRole.MEMBER)


def test_update_rechecks_a_demoted_role_under_the_lock(make_caller: MakeCaller) -> None:
    """The dependency saw Admin; by the time the change runs the caller is a Member: refused."""
    owner, admin = make_caller("owner@example.com"), make_caller("admin@example.com")
    project_id = _create(owner)
    with get_session_factory()() as session:
        session.add(ProjectMember(project_id=project_id, user_id=admin.user.id, role="admin"))
        session.commit()
    stale = ProjectAccess(project_id=project_id, user_id=admin.user.id, role=ProjectRole.ADMIN)
    _set_role(project_id, admin.user.id, ProjectRole.MEMBER)

    with get_session_factory()() as session:
        with pytest.raises(InsufficientProjectRoleError):
            service.update_project(session, access=stale, changes={"name": "Changed"})
        assert not session.in_transaction()

    with get_session_factory()() as session:
        assert session.get_one(Project, project_id).name == "Project"


def test_update_rechecks_a_removed_membership_under_the_lock(make_caller: MakeCaller) -> None:
    owner = make_caller("owner@example.com")
    project_id = _create(owner)
    stale = ProjectAccess(project_id=project_id, user_id=owner.user.id, role=ProjectRole.OWNER)
    with get_session_factory()() as session:
        session.delete(session.get_one(ProjectMember, (project_id, owner.user.id)))
        session.commit()

    with get_session_factory()() as session, pytest.raises(ProjectNotFoundError):
        service.update_project(session, access=stale, changes={"name": "Changed"})


def test_delete_rechecks_ownership_under_the_lock(make_caller: MakeCaller) -> None:
    owner, admin = make_caller("owner@example.com"), make_caller("admin@example.com")
    project_id = _create(owner)
    with get_session_factory()() as session:
        session.add(ProjectMember(project_id=project_id, user_id=admin.user.id, role="admin"))
        session.commit()
    stale = ProjectAccess(project_id=project_id, user_id=owner.user.id, role=ProjectRole.OWNER)
    # Ownership moved away (as a later transfer would do: demote first, then promote).
    _set_role(project_id, owner.user.id, ProjectRole.ADMIN)
    _set_role(project_id, admin.user.id, ProjectRole.OWNER)

    with get_session_factory()() as session, pytest.raises(InsufficientProjectRoleError):
        service.delete_project(session, access=stale, confirm_name="Project")

    assert _count(Project) == 1


def test_update_and_delete_of_a_vanished_project(make_caller: MakeCaller) -> None:
    owner = make_caller("owner@example.com")
    project_id = _create(owner)
    access = ProjectAccess(project_id=project_id, user_id=owner.user.id, role=ProjectRole.OWNER)
    with get_session_factory()() as session:
        session.delete(session.get_one(Project, project_id))
        session.commit()

    with get_session_factory()() as session:
        with pytest.raises(ProjectNotFoundError):
            service.update_project(session, access=access, changes={"name": "x"})
        with pytest.raises(ProjectNotFoundError):
            service.delete_project(session, access=access, confirm_name="Project")


@pytest.mark.parametrize("target", ["update", "delete"])
def test_failures_roll_back(make_caller: MakeCaller, monkeypatch: pytest.MonkeyPatch, target: str) -> None:
    owner = make_caller("owner@example.com")
    project_id = _create(owner)
    access = ProjectAccess(project_id=project_id, user_id=owner.user.id, role=ProjectRole.OWNER)

    with get_session_factory()() as session:
        if target == "update":
            original_commit = session.commit
            monkeypatch.setattr(session, "commit", _boom)
            with pytest.raises(RuntimeError):
                service.update_project(session, access=access, changes={"name": "Changed"})
            monkeypatch.setattr(session, "commit", original_commit)
        else:
            monkeypatch.setattr(repository, "delete_project", _boom)
            with pytest.raises(RuntimeError):
                service.delete_project(session, access=access, confirm_name="Project")
        assert not session.in_transaction()

    with get_session_factory()() as session:
        project = session.get_one(Project, project_id)
        assert project.name == "Project"
        assert session.scalar(select(func.count()).select_from(ProjectMember)) == 1


def test_confirmation_mismatch_rolls_back(make_caller: MakeCaller) -> None:
    owner = make_caller("owner@example.com")
    project_id = _create(owner)
    access = ProjectAccess(project_id=project_id, user_id=owner.user.id, role=ProjectRole.OWNER)

    with get_session_factory()() as session:
        with pytest.raises(DeletionConfirmationError):
            service.delete_project(session, access=access, confirm_name="project")
        assert not session.in_transaction()

    assert _count(Project) == 1


def test_list_projects_is_read_only(make_caller: MakeCaller) -> None:
    owner = make_caller("owner@example.com")
    _create(owner)

    with get_session_factory()() as session:
        pending = Project(name="Pending")
        session.add(pending)
        assert len(service.list_projects(session, user_id=owner.user.id, limit=10, offset=0)) == 1
        assert pending in session.new
        session.rollback()
