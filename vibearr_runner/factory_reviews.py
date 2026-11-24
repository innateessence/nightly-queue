"""Current-candidate gates, routed reviews and non-authorizing decisions."""

from copy import deepcopy
from dataclasses import asdict
import hashlib
from pathlib import Path
from uuid import uuid4

from .factory_contract import FactoryContractError, criteria, validate_payload
from .factory_routing import (
    FactoryRoutingError,
    changed_paths,
    require_route_capabilities,
    route,
)
from .native_contract import receipt_summary, validate_execution_identity
from .native_sessions import NativeSessionError
from .native_validation import CandidateSource
from .recovery import ReconciliationRequired
from .workspaces import git


class FactoryReviewError(NativeSessionError):
    pass


def collect_factory_paths(checkout, base_sha, head_sha):
    inventory = git(
        [
            "diff",
            "--no-renames",
            "--name-status",
            "-z",
            base_sha,
            head_sha,
            "--",
        ],
        checkout,
    )
    fields = inventory.split("\x00")
    if fields[-1] != "":
        raise FactoryReviewError("Changed-path inventory is incomplete.")
    fields.pop()
    if len(fields) % 2 or any(
        status not in {"A", "D", "M", "T"} for status in fields[::2]
    ):
        raise FactoryReviewError("Changed-path inventory has an unexpected status.")
    try:
        return changed_paths(fields[1::2])
    except FactoryRoutingError as exc:
        raise FactoryReviewError(str(exc)) from None


class FactoryReviewLoop:
    def __init__(self, factory):
        self.factory = factory
        self.host = factory.host
        self.state = factory.state

    @property
    def round(self):
        number = self.state.get("round")
        if type(number) is not int or number < 1:
            raise FactoryReviewError("Factory round is missing or invalid.")
        return number

    def key(self, role):
        return f"{role}:{self.round}"

    def source(self):
        candidate = self.host.native["candidate"]
        source = CandidateSource.from_git(
            self.host.workspace().checkout, candidate["workspace"]["commit_sha"]
        )
        if source.digest != candidate["source_digest"]:
            raise FactoryReviewError("Factory candidate changed after import.")
        return source

    def artifact(self, role):
        saved = self.state.get("artifacts", {}).get(self.key(role))
        if saved is None:
            return None
        self.checked(saved, role)
        return saved

    def checked(self, saved, role):
        source = self.source()
        context, plan = self.host.native["context"], self.host.native["plan"]
        from .native_contract import digest

        expected = {
            "run_id": self.host.run_id,
            "role": role,
            "round": self.round,
            "source_digest": source.digest,
            "tree_sha": source.tree_sha,
            "head_sha": source.commit_sha,
            "base_sha": self.host.native["candidate"]["workspace"]["base_revision"],
            "context_id": context["id"],
            "context_digest": context["digest"],
            "specification_id": plan["id"],
            "specification_digest": plan["digest"],
            "configuration_digest": digest(self.host.snapshot),
        }
        document = saved.get("document") if isinstance(saved, dict) else None
        if not isinstance(document, dict) or any(
            document.get(key) != value for key, value in expected.items()
        ):
            raise FactoryReviewError(
                "Factory evidence belongs to another candidate or round."
            )
        try:
            validate_payload(role, document.get("payload"))
        except FactoryContractError as exc:
            raise FactoryReviewError(str(exc)) from None
        payload = document["payload"]
        payload_fields = None
        if role.startswith("reviewer-"):
            payload_fields = {"verdict", "findings", "coverage"}
            if any(
                set(finding) != {"id", "summary", "evidence", "criterion_ids"}
                for finding in payload["findings"]
            ):
                raise FactoryReviewError(
                    "Review findings must use the exact result schema."
                )
        if role == "decision":
            payload_fields = {"verdict", "rationale", "artifact_ids"}
        if payload_fields is not None and set(payload) != payload_fields:
            raise FactoryReviewError("Factory role must use the exact result schema.")
        return document["payload"]

    def candidate_imported(self, source):
        key = self.key("build")
        data = self.state.get("roles", {}).get(key, {})
        if not data.get("successful") or not data.get("execution"):
            raise FactoryReviewError(
                "Imported factory candidate lacks its builder receipt."
            )
        candidate = self.host.native["candidate"]
        if candidate.get("agent_intent_id") != data.get("intent_id") or candidate.get(
            "export_digest"
        ) != data.get("export_digest"):
            raise FactoryReviewError(
                "Imported factory candidate differs from its builder receipt."
            )
        mapping = data["role_result"]["criteria"]
        if not isinstance(mapping, (list, dict)) or any(
            not isinstance(key, str) for key in mapping
        ):
            raise FactoryReviewError(
                "Builder must map every saved specification criterion."
            )
        if set(mapping) != self.criterion_ids() or len(mapping) != len(set(mapping)):
            raise FactoryReviewError(
                "Builder must map every saved specification criterion."
            )
        if isinstance(mapping, dict) and any(
            not isinstance(value, str) or not value.strip()
            for value in mapping.values()
        ):
            raise FactoryReviewError(
                "Builder criterion mappings must explain the implemented changes."
            )
        if self.artifact("build") is None:
            self.factory.save_artifact(
                "build",
                data["role_result"],
                key=key,
                execution=data["execution"],
                source=source,
            )
        self.state["stage"] = "awaiting-gate"
        self.host.save()

    def validated(self, observed):
        state = observed.get("state")
        if state not in {"passed", "failed"}:
            raise FactoryReviewError("Record a terminal independent check result.")
        if self.artifact("build") is None:
            raise FactoryReviewError("Checks lack their imported builder artifact.")
        validation = self.host.native.get("validation", {})
        result_id = validation.get("result_id")
        if not result_id:
            raise FactoryReviewError("Checks lack their protected validation receipt.")
        self.factory.save_artifact(
            "checks",
            {"validation_id": result_id, "state": state},
            key=self.key("checks"),
            source=self.source(),
        )
        if state != "passed":
            operation = observed.get("operation", {}).get("operation_id")
            logs = self.host.executor.logs(operation, 0, 16000) if operation else b""
            diagnostics = f"Check {observed.get('check_id', 'unknown')} failed.\n"
            return self.repair("checks", diagnostics + logs.decode(errors="replace"))
        self.state["stage"] = "review"
        self.host.native["phase"] = "factory"
        self.host.save()
        return "continue"

    def repair(self, reason, diagnostics):
        from .factory_recovery import FactoryRecovery

        if reason == "checks":
            saved = self.artifact("checks")
            validation = self.host.native.get("validation", {})
            if saved is None or saved["document"]["payload"] != {
                "validation_id": validation.get("result_id"),
                "state": "failed",
            }:
                raise FactoryReviewError(
                    "Factory repair requires the actual failed check artifact."
                )
            records = [saved]
        elif reason == "reviews":
            records = [self.require_gate(), self.artifact("routing")]
            verdicts = []
            for role in self.routing()["required_reviewers"]:
                saved = self.artifact(role)
                if saved is None:
                    raise FactoryReviewError(
                        "Factory repair requires every current routed review."
                    )
                verdicts.append(self.checked_review(saved, role)["verdict"])
                records.append(saved)
            if "changes" not in verdicts or "blocked" in verdicts:
                raise FactoryReviewError(
                    "Factory repair requires concrete current review changes."
                )
        else:
            raise FactoryReviewError(
                "Choose an actual failed check or review repair trigger."
            )
        self.state["repair_trigger"] = {
            "reason": reason,
            "round": self.round,
            "artifact_ids": [record["id"] for record in records if record is not None],
            "source_digest": self.source().digest,
            "specification_id": self.host.native["plan"]["id"],
            "specification_digest": self.host.native["plan"]["digest"],
        }
        self.host.save()
        return FactoryRecovery(self.host, self.state).repair(reason, diagnostics)

    def step(self):
        stage = self.state.get("stage")
        if stage == "build":
            return self.build()
        if stage == "review":
            return self.review()
        if stage == "decision":
            return self.decision()
        raise FactoryReviewError(
            "Factory review stage cannot advance without its gate."
        )

    def build(self):
        result = self.factory.run_role(
            "build",
            key=self.key("build"),
            payload={
                "specification": self.factory.specification_payload(),
                "specification_plan": self.host.native["plan"]["document"],
                "criterion_ids": sorted(self.criterion_ids()),
                "diagnostics": self.host.native.get("repair_diagnostic", ""),
            },
        )
        if result is None:
            return "running"
        if result.get("build_complete") is not True:
            raise FactoryReviewError(
                "Builder did not return its protected candidate export."
            )
        self.host.native["agent"] = deepcopy(self.state["roles"][self.key("build")])
        self.host.native["phase"] = "import_candidate"
        self.state["stage"] = "awaiting-gate"
        self.host.save()
        return "continue"

    def routing(self):
        saved = self.artifact("routing")
        if saved is not None:
            routing = saved["document"]["payload"]
            require_route_capabilities(routing, self.host.executor.capabilities())
            return routing
        source = self.source()
        paths = self.host.native["candidate"].get("factory_paths")
        try:
            if paths != changed_paths(paths):
                raise FactoryRoutingError("Factory path attestation must be canonical.")
        except FactoryRoutingError as exc:
            raise FactoryReviewError(str(exc)) from None
        intake = (
            self.state.get("artifacts", {})
            .get("intake", {})
            .get("document", {})
            .get("payload")
        )
        if not isinstance(intake, dict) or intake.get("verdict") != "accept":
            raise FactoryReviewError(
                "Review routing requires the saved accepted intake."
            )
        configuration = self.host.snapshot["effective"]["factory"]
        previous = None
        for number in range(self.round - 1, 0, -1):
            saved = self.state.get("artifacts", {}).get(f"routing:{number}")
            if saved is not None:
                previous = saved["document"]["payload"]
                break
        try:
            routing = route(configuration, intake, paths, previous=previous)
        except FactoryRoutingError as exc:
            raise FactoryReviewError(str(exc)) from None
        if routing["protected_paths"]:
            raise FactoryReviewError(
                "Candidate changes protected validation or runtime policy paths."
            )
        require_route_capabilities(routing, self.host.executor.capabilities())
        self.factory.save_artifact(
            "routing", routing, key=self.key("routing"), source=source
        )
        return routing

    def require_gate(self):
        checks = self.artifact("checks")
        if checks is None or checks["document"]["payload"]["state"] != "passed":
            raise FactoryReviewError(
                "Reviews require passing independent checks for this candidate."
            )
        validation = self.host.native.get("validation", {})
        if not validation.get("evidence"):
            raise FactoryReviewError("Passing checks lack native validation evidence.")
        session = validation.get("session", {})
        source = self.source()
        if checks["document"]["payload"]["validation_id"] != validation.get(
            "result_id"
        ) or any(
            session.get(key) != value
            for key, value in {
                "run_id": self.host.run_id,
                "source_digest": source.digest,
                "tree_sha": source.tree_sha,
            }.items()
        ):
            raise FactoryReviewError(
                "Passing checks differ from the current protected validation receipt."
            )
        return checks

    def candidate_diff(self):
        source = self.source()
        base = self.host.native["candidate"]["workspace"]["base_revision"]
        result = git(
            [
                "diff",
                "--no-ext-diff",
                "--no-textconv",
                "--no-renames",
                "--unified=3",
                base,
                source.commit_sha,
                "--",
            ],
            self.host.workspace().checkout,
        )
        if len(result.encode()) > 128 * 1024:
            raise FactoryReviewError(
                "Candidate diff exceeds the complete review input limit."
            )
        return {"base_sha": base, "head_sha": source.commit_sha, "patch": result}

    def criterion_ids(self):
        specification = (
            self.state.get("artifacts", {})
            .get("specification", {})
            .get("document", {})
            .get("payload", {})
        )
        try:
            return set(criteria(specification.get("criteria")))
        except FactoryContractError as exc:
            raise FactoryReviewError(str(exc)) from None

    def review(self):
        checks = self.require_gate()
        routing = self.routing()
        difference = self.candidate_diff()
        browser = None
        if any(
            role in routing["required_reviewers"]
            for role in ("reviewer-ux", "reviewer-ui")
        ):
            browser = FactoryBrowserCapture(self.host, self.state).step(self.source())
            if browser is None:
                return "running"
        reviewed = []
        for role in routing["required_reviewers"]:
            saved = self.artifact(role)
            if saved is None:
                saved = self.factory.run_role(
                    role,
                    key=self.key(role),
                    payload={
                        "specification": self.factory.specification_payload(),
                        "specification_plan": self.host.native["plan"]["document"],
                        "candidate_diff": difference,
                        "validation_evidence": self.host.native["validation"][
                            "evidence"
                        ],
                        "build": self.artifact("build"),
                        "checks": checks,
                        "routing": routing,
                        "browser_capture": (
                            {
                                name: browser[name]
                                for name in ("operation_id", "archive_digest")
                            }
                            if browser and role in {"reviewer-ux", "reviewer-ui"}
                            else None
                        ),
                        "browser_receipts": browser["receipts"] if browser else [],
                    },
                    source=self.source(),
                )
                if saved is None:
                    return "running"
            reviewed.append((role, saved, self.checked_review(saved, role)))
        diagnostics = []
        blocked = False
        for role, _saved, payload in reviewed:
            blocked = blocked or payload["verdict"] == "blocked"
            for finding in payload["findings"]:
                diagnostics.append(
                    f"{role} {finding['id']}: {finding['summary']}\n"
                    + "\n".join(finding["evidence"])
                )
        if blocked:
            raise FactoryReviewError(
                "Required review blocked: " + "\n".join(diagnostics)[:16000]
            )
        if any(payload["verdict"] != "pass" for _role, _saved, payload in reviewed):
            return self.repair("reviews", "\n".join(diagnostics)[:16000])
        self.state["stage"] = "decision"
        self.host.save()
        return "continue"

    def checked_review(self, saved, role):
        payload = self.checked(saved, role)
        required = self.criterion_ids()
        coverage = payload["coverage"]
        if len(coverage) != len(set(coverage)) or not set(coverage) <= required:
            raise FactoryReviewError("Reviewer criterion coverage is invalid.")
        if any(
            not set(finding["criterion_ids"]) <= required
            for finding in payload["findings"]
        ):
            raise FactoryReviewError("Review finding references an unknown criterion.")
        if payload["verdict"] == "pass" and (
            set(coverage) != required or payload["findings"]
        ):
            raise FactoryReviewError(
                "Passing review must cover every criterion without unresolved findings."
            )
        if payload["verdict"] != "pass" and not payload["findings"]:
            raise FactoryReviewError(
                "A nonpassing review must explain a concrete finding."
            )
        return payload

    def decision(self):
        checks = self.require_gate()
        routing = self.routing()
        build = self.artifact("build")
        if build is None:
            raise FactoryReviewError(
                "Decision requires the current imported builder artifact."
            )
        records = [checks, self.artifact("routing")]
        for role in routing["required_reviewers"]:
            saved = self.artifact(role)
            if saved is None or self.checked_review(saved, role)["verdict"] != "pass":
                raise FactoryReviewError(
                    "Decision requires every current routed reviewer to pass."
                )
            records.append(saved)
        required = {record["id"] for record in records if record is not None}
        saved = self.artifact("decision")
        if saved is None:
            saved = self.factory.run_role(
                "decision",
                key=self.key("decision"),
                payload={"build": build, "required_artifacts": records},
                source=self.source(),
            )
            if saved is None:
                return "running"
        payload = self.checked(saved, "decision")
        if set(payload["artifact_ids"]) != required or len(
            payload["artifact_ids"]
        ) != len(required):
            raise FactoryReviewError(
                "Decision must reference the complete current gate and review set."
            )
        if payload["verdict"] != "ready":
            raise FactoryReviewError(
                "Factory decision is unresolved: " + payload["rationale"][:2000]
            )
        self.state["current_decision"] = saved["id"]
        self.state["stage"] = "delivered-ready"
        self.host.native["phase"] = "delivery"
        self.host.save()
        return "continue"


class FactoryBrowserCapture:
    def __init__(self, host, factory_state):
        self.host = host
        self.state = factory_state

    def step(self, source):
        from vibearr_executor.contracts import Profile, canonical

        capabilities = self.host.executor.capabilities()
        profiles = capabilities.get("profiles", [])
        registered = next(
            (item for item in profiles if item.get("profile_id") == "browser-ui"), None
        )
        if registered is None:
            raise FactoryReviewError("Required worker browser profile is unavailable.")
        try:
            profile = Profile.parse(
                {key: registered[key] for key in Profile.__dataclass_fields__}
            )
        except (KeyError, ValueError, TypeError):
            raise FactoryReviewError("Worker browser profile is malformed.") from None
        if (
            profile.phase != "validation"
            or profile.network_hosts
            or profile.backend_secret_env
        ):
            raise FactoryReviewError(
                "Browser checks require an offline, credential-free registered profile."
            )
        if registered.get("profile_digest") != profile.digest:
            raise FactoryReviewError("Worker browser profile changed.")
        allowed = {
            "cpu": self.host.policy.cpu_limit,
            "memory_mb": self.host.policy.memory_mb,
            "pids": self.host.policy.pids_limit,
            "workspace_mb": self.host.policy.workspace_mb,
        }
        if any(value > allowed[name] for name, value in asdict(profile.limits).items()):
            raise FactoryReviewError(
                "Registered browser limits exceed the frozen project policy."
            )
        binding = {
            "schema_version": 1,
            "revision": source.commit_sha,
            "tree_sha": source.tree_sha,
            "source_digest": source.digest,
            "application_image": profile.image,
            "browser_image": profile.image,
        }
        key = str(self.state["round"])
        captures = self.state.setdefault("browser", {})
        data = captures.get(key)
        if data is None:
            operation = str(uuid4())
            data = {"operation_id": operation, "binding": binding}
            captures[key] = data
            self.host.save()
        if data["binding"] != binding:
            raise FactoryReviewError(
                "Browser operation belongs to a different candidate."
            )
        operation = data["operation_id"]
        if "manifest" not in data:
            context = {"browser_binding": binding}
            self.host.executor.upload("source", source.archive)
            context_digest = self.host.executor.upload("context", canonical(context))[
                "sha256"
            ]
            data["manifest"] = {
                "schema_version": 1,
                "run_id": self.host.run_id,
                "operation_id": operation,
                "profile_id": profile.profile_id,
                "profile_digest": profile.digest,
                "source_digest": source.digest,
                "context_digest": context_digest,
                "phase": "validation",
                "limits": asdict(profile.limits),
                "deadline": self.host.stage_deadline("validation_timeout_seconds"),
                "network_hosts": [],
            }
            self.host.resources.reserve(self.host.run_id, operation)
            self.host.save()
        prepared = self.host.executor.prepare(data["manifest"])
        identity = validate_execution_identity(prepared.get("execution_identity"))
        if data.get("execution_identity") not in (None, identity):
            raise ReconciliationRequired("Browser operation worker identity changed.")
        data["execution_identity"] = identity
        self.host.save()
        self.host.resources.operation(
            self.host.run_id, data["manifest"], kind="validation"
        )
        observed = self.host.executor.status(operation)
        if observed.get("operation_id") != operation:
            raise FactoryReviewError("Browser status belongs to another operation.")
        if observed.get("state") == "unknown":
            raise ReconciliationRequired(
                "Browser operation is uncertain; retain its original identity."
            )
        if observed.get("execution_identity") != identity:
            raise ReconciliationRequired(
                "Browser status belongs to another worker instance or boot."
            )
        if observed.get("state") == "not-started":
            if not self.host.checkpoint(force_source=True):
                return None
            self.host.resources.dispatch(self.host.run_id, operation)
            self.host.executor.start(operation)
            return None
        if observed.get("state") == "running":
            self.host.status(
                "validation", detail="Rendering the current candidate for UX/UI review."
            )
            return None
        receipt = receipt_summary(observed)
        result = observed.get("result", {})
        if (
            result.get("exit_code") != 0
            or not observed.get("descendants_terminated")
            or any(
                result.get(name)
                for name in (
                    "canceled",
                    "timed_out",
                    "log_truncated",
                    "error",
                    "export_error",
                )
            )
        ):
            raise FactoryReviewError(
                "Required browser capture failed or did not stop cleanly."
            )
        if receipt.get("run_id") != self.host.run_id or any(
            receipt.get(name) != source.digest
            for name in ("source_digest", "export_digest")
        ):
            raise FactoryReviewError(
                "Browser capture changed its candidate source or run binding."
            )
        identity = receipt.get("execution_identity")
        if not isinstance(identity, dict) or identity != data["execution_identity"]:
            raise FactoryReviewError(
                "Browser capture lacks its actual worker identity."
            )
        worker_binding = {
            "run_id": self.host.run_id,
            "operation_id": operation,
            "execution_identity": identity,
            "profile_id": profile.profile_id,
            "profile_digest": profile.digest,
        }
        report, archive = self.host.executor.browser_result(operation)
        from .browser_receipts import import_worker_capture

        root = Path(self.host.journal.directory) / "factory-browser" / self.host.run_id
        try:
            frames = import_worker_capture(
                report,
                archive,
                binding=binding,
                worker_binding=worker_binding,
                artifact_root=root,
            )
        except (ValueError, OSError) as exc:
            raise FactoryReviewError(
                "Browser capture does not match the current candidate: " + str(exc)
            ) from None
        data["receipts"] = frames
        data["archive_digest"] = hashlib.sha256(archive).hexdigest()
        self.host.save()
        return {
            "operation_id": operation,
            "archive_digest": data["archive_digest"],
            "receipts": frames,
        }
