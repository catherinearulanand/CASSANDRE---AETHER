from app.core import services as _services
from app.engines.evidence import service  # noqa: F401  (registers evidence.append / verify_chain)
from app.engines.evidence.bundle import build_bundle
from app.engines.evidence.checkpoints import create_checkpoint, verify_against_exported
from app.engines.evidence.lineage import record_lineage
from app.engines.evidence.objects import read_object, store_object
from app.engines.evidence.replay import replay_run

_services.register("evidence.store_object", store_object)
_services.register("evidence.read_object", read_object)
_services.register("evidence.build_bundle", build_bundle)
_services.register("evidence.create_checkpoint", create_checkpoint)
_services.register("evidence.verify_against_exported", verify_against_exported)
_services.register("evidence.record_lineage", record_lineage)
_services.register("evidence.replay_run", replay_run)
