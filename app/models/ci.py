from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.sqlite import JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Pipeline(Base):
    __tablename__ = "pipelines"
    __table_args__ = (
        UniqueConstraint("project_id", "iid", name="uq_pipeline_project_iid"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("repositories.id"), nullable=False
    )
    iid: Mapped[int] = mapped_column(Integer, nullable=False)
    ref: Mapped[str] = mapped_column(String, nullable=False)
    sha: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, default="pending")
    source: Mapped[str] = mapped_column(String, default="api")
    security_warnings: Mapped[list] = mapped_column(JSON, default=list)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    project = relationship("Repository", lazy="selectin")
    jobs = relationship(
        "PipelineJob",
        back_populates="pipeline",
        lazy="selectin",
        cascade="all, delete-orphan",
    )


class PipelineTrigger(Base):
    __tablename__ = "pipeline_triggers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("repositories.id"), nullable=False
    )
    description: Mapped[str] = mapped_column(String, default="")
    token: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    owner_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=True
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    project = relationship("Repository", lazy="selectin")
    owner = relationship("User", lazy="selectin")


class PipelineSchedule(Base):
    __tablename__ = "pipeline_schedules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("repositories.id"), nullable=False
    )
    description: Mapped[str] = mapped_column(String, default="")
    ref: Mapped[str] = mapped_column(String, default="main")
    cron: Mapped[str] = mapped_column(String, default="0 0 * * *")
    cron_timezone: Mapped[str] = mapped_column(String, default="UTC")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    variables: Mapped[list] = mapped_column(JSON, default=list)
    owner_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=True
    )
    last_pipeline_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("pipelines.id"), nullable=True
    )
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    project = relationship("Repository", lazy="selectin")
    owner = relationship("User", lazy="selectin")
    last_pipeline = relationship("Pipeline", lazy="selectin")


class CiVariable(Base):
    __tablename__ = "ci_variables"
    __table_args__ = (
        UniqueConstraint(
            "scope_type",
            "scope_id",
            "key",
            "environment_scope",
            name="uq_ci_variable_scope_key_environment",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scope_type: Mapped[str] = mapped_column(String, nullable=False)
    scope_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    key: Mapped[str] = mapped_column(String, nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    variable_type: Mapped[str] = mapped_column(String, default="env_var")
    visibility: Mapped[str] = mapped_column(String, default="visible")
    protected: Mapped[bool] = mapped_column(Boolean, default=False)
    raw: Mapped[bool] = mapped_column(Boolean, default=False)
    environment_scope: Mapped[str] = mapped_column(String, default="*")
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class CiSecret(Base):
    __tablename__ = "ci_secrets"
    __table_args__ = (
        UniqueConstraint(
            "scope_type",
            "scope_id",
            "name",
            "environment_scope",
            "branch_scope",
            name="uq_ci_secret_scope_name_environment_branch",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scope_type: Mapped[str] = mapped_column(String, nullable=False)
    scope_id: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    environment_scope: Mapped[str] = mapped_column(String, default="*")
    branch_scope: Mapped[str] = mapped_column(String, default="*")
    protected: Mapped[bool] = mapped_column(Boolean, default=False)
    rotation_reminder_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String, default="healthy")
    last_accessed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_accessed_by_job_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("pipeline_jobs.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    last_accessed_by_job = relationship("PipelineJob", lazy="selectin")


class CiSecretAccessEvent(Base):
    __tablename__ = "ci_secret_access_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    secret_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("ci_secrets.id"), nullable=False
    )
    project_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("repositories.id"), nullable=False
    )
    pipeline_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("pipelines.id"), nullable=False
    )
    job_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("pipeline_jobs.id"), nullable=False
    )
    ref: Mapped[str] = mapped_column(String, nullable=False)
    environment: Mapped[str | None] = mapped_column(String, nullable=True)
    accessed_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    secret = relationship("CiSecret", lazy="selectin")
    project = relationship("Repository", lazy="selectin")
    pipeline = relationship("Pipeline", lazy="selectin")
    job = relationship("PipelineJob", lazy="selectin")


class CiRunner(Base):
    __tablename__ = "ci_runners"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    token: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    description: Mapped[str] = mapped_column(String, default="glemu-runner")
    tags: Mapped[list] = mapped_column(JSON, default=list)
    run_untagged: Mapped[bool] = mapped_column(Boolean, default=True)
    paused: Mapped[bool] = mapped_column(Boolean, default=False)
    locked: Mapped[bool] = mapped_column(Boolean, default=False)
    runner_name: Mapped[str | None] = mapped_column(String, nullable=True)
    runner_version: Mapped[str | None] = mapped_column(String, nullable=True)
    runner_revision: Mapped[str | None] = mapped_column(String, nullable=True)
    runner_platform: Mapped[str | None] = mapped_column(String, nullable=True)
    runner_architecture: Mapped[str | None] = mapped_column(String, nullable=True)
    runner_executor: Mapped[str | None] = mapped_column(String, nullable=True)
    system_id: Mapped[str | None] = mapped_column(String, nullable=True)
    last_contact_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_poll_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_verify_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_job_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("pipeline_jobs.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    last_job = relationship("PipelineJob", lazy="selectin")


class PipelineJob(Base):
    __tablename__ = "pipeline_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    pipeline_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("pipelines.id"), nullable=False
    )
    project_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("repositories.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    stage: Mapped[str] = mapped_column(String, default="test")
    stage_index: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String, default="pending")
    image: Mapped[str] = mapped_column(String, default="alpine:3.20")
    script: Mapped[list] = mapped_column(JSON, default=list)
    variables: Mapped[dict] = mapped_column(JSON, default=dict)
    needs: Mapped[list] = mapped_column(JSON, default=list)
    tags: Mapped[list] = mapped_column(JSON, default=list)
    cache: Mapped[list] = mapped_column(JSON, default=list)
    artifacts_paths: Mapped[list] = mapped_column(JSON, default=list)
    artifacts_config: Mapped[dict] = mapped_column(JSON, default=dict)
    when: Mapped[str] = mapped_column(String, default="on_success")
    allow_failure: Mapped[bool] = mapped_column(Boolean, default=False)
    retry_config: Mapped[dict] = mapped_column(JSON, default=dict)
    timeout_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    interruptible: Mapped[bool] = mapped_column(Boolean, default=False)
    resource_group: Mapped[str | None] = mapped_column(String, nullable=True)
    coverage_regex: Mapped[str | None] = mapped_column(String, nullable=True)
    secret_metadata: Mapped[list] = mapped_column(JSON, default=list)
    job_token: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    runner_name: Mapped[str | None] = mapped_column(String, nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    trace_checksum: Mapped[str | None] = mapped_column(String, nullable=True)
    trace_size: Mapped[int] = mapped_column(Integer, default=0)

    queued_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    pipeline = relationship("Pipeline", back_populates="jobs", lazy="selectin")
    project = relationship("Repository", lazy="selectin")
    trace = relationship(
        "JobTrace",
        back_populates="job",
        uselist=False,
        lazy="selectin",
        cascade="all, delete-orphan",
    )
    artifacts = relationship(
        "JobArtifact",
        back_populates="job",
        lazy="selectin",
        cascade="all, delete-orphan",
    )


class JobTrace(Base):
    __tablename__ = "job_traces"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("pipeline_jobs.id"), unique=True, nullable=False
    )
    content: Mapped[str] = mapped_column(Text, default="")
    size: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    job = relationship("PipelineJob", back_populates="trace", lazy="selectin")


class JobArtifact(Base):
    __tablename__ = "job_artifacts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("pipeline_jobs.id"), nullable=False
    )
    filename: Mapped[str] = mapped_column(String, default="artifacts.zip")
    content_type: Mapped[str | None] = mapped_column(String, nullable=True)
    file_type: Mapped[str] = mapped_column(String, default="archive")
    file_format: Mapped[str] = mapped_column(String, default="zip")
    size: Mapped[int] = mapped_column(Integer, default=0)
    storage_path: Mapped[str | None] = mapped_column(String, nullable=True)
    expire_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    job = relationship("PipelineJob", back_populates="artifacts", lazy="selectin")
