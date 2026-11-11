"""Candidate manifest adapter and image/runtime binding.

The builder owns the real ``fnos-x5-assembly/v1`` manifest.  This consumer
normalizes it into the lab candidate contract without editing the builder's
file.  The assembly manifest does not carry the trim version, so an explicit
``product_version`` adapter value is required; an ordinary Debian os-release
can never substitute for the fnOS product layer.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import hashlib
import re
from typing import Any

from .errors import Refused
from .runtime_identity import IdentityExpectation, RuntimeIdentity, require_identity
from .util import read_json

LAB_SCHEMA = "fnos-lab-candidate/v1"
ASSEMBLY_SCHEMA = "fnos-x5-assembly/v1"
ASSEMBLY_V2_SCHEMA = "fnos-x5-assembly/v2"
SHA_RE = re.compile(r"^[0-9a-fA-F]{64}$")
UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z._-]+)?$")


@dataclass(frozen=True)
class CandidateManifest:
    schema: str
    candidate_id: str
    board_model: str
    base_os_id: str
    base_os_version: str
    kernel_release: str
    fnos_id: str
    fnos_version: str
    root_uuid: str
    image_sha256: str
    image_size_bytes: int
    source: str
    created_at: str
    accepted: bool = False

    def expectation(self) -> IdentityExpectation:
        return IdentityExpectation(
            board_model_contains=self.board_model,
            base_os_id=self.base_os_id or None,
            base_os_version=self.base_os_version or None,
            kernel_release=self.kernel_release,
            fnos_id=self.fnos_id,
            fnos_version=self.fnos_version,
            root_uuid=self.root_uuid,
            require_serial=False,
            require_version_file=self.schema == "fnos-x5-assembly/v2",
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "candidate_id": self.candidate_id,
            "board_model": self.board_model,
            "base_os_id": self.base_os_id,
            "base_os_version": self.base_os_version,
            "kernel_release": self.kernel_release,
            "fnos_id": self.fnos_id,
            "fnos_version": self.fnos_version,
            "root_uuid": self.root_uuid,
            "image_sha256": self.image_sha256,
            "image_size_bytes": self.image_size_bytes,
            "source": self.source,
            "created_at": self.created_at,
            "accepted": self.accepted,
        }


def _require_string(raw: dict[str, Any], field: str) -> str:
    value = raw.get(field)
    if not isinstance(value, str) or not value.strip():
        raise Refused(f"candidate manifest field {field} is missing/empty")
    return value


def _normalise_lab(raw: dict[str, Any]) -> CandidateManifest:
    image_sha = _require_string(raw, "image_sha256").lower()
    if not SHA_RE.fullmatch(image_sha):
        raise Refused("candidate manifest image_sha256 must be 64 hex characters")
    size = raw.get("image_size_bytes")
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        raise Refused("candidate manifest image_size_bytes must be positive")
    root_uuid = _require_string(raw, "root_uuid")
    if not UUID_RE.fullmatch(root_uuid):
        raise Refused("candidate manifest root_uuid is malformed")
    fnos_id = _require_string(raw, "fnos_id")
    if fnos_id.lower() != "fnos":
        raise Refused("candidate manifest fnos_id must be fnos")
    fnos_version = _require_string(raw, "fnos_version")
    if not VERSION_RE.fullmatch(fnos_version):
        raise Refused("candidate manifest fnos_version is malformed")
    return CandidateManifest(
        schema=LAB_SCHEMA,
        candidate_id=_require_string(raw, "candidate_id"),
        board_model=_require_string(raw, "board_model"),
        base_os_id=str(raw.get("base_os_id") or ""),
        base_os_version=str(raw.get("base_os_version") or ""),
        kernel_release=_require_string(raw, "kernel_release"),
        fnos_id=fnos_id,
        fnos_version=fnos_version,
        root_uuid=root_uuid,
        image_sha256=image_sha,
        image_size_bytes=size,
        source=_require_string(raw, "source"),
        created_at=_require_string(raw, "created_at"),
        # Legacy lab manifests predate the acceptance field; tests/explicit
        # review records set accepted=true.  They are not the real assembly.
        accepted=raw.get("accepted") is True,
    )


def _normalise_assembly(raw: dict[str, Any], product_version: str | None) -> CandidateManifest:
    product = raw.get("product") if isinstance(raw.get("product"), dict) else {}
    manifest_version = product.get("trim_version")
    if isinstance(manifest_version, str) and manifest_version.strip():
        if product_version and product_version != manifest_version:
            raise Refused(
                "assembly manifest product.trim_version conflicts with --product-version"
            )
        product_version = manifest_version
    if not isinstance(product_version, str) or not VERSION_RE.fullmatch(product_version):
        raise Refused("assembly adapter product_version must look like 1.2.0701")
    output = raw.get("output")
    rootfs = raw.get("rootfs")
    if not isinstance(output, dict) or not isinstance(rootfs, dict):
        raise Refused("assembly manifest is missing output/rootfs")
    sha = output.get("sha256")
    size = output.get("size")
    root_uuid = rootfs.get("uuid")
    if not isinstance(sha, str) or not SHA_RE.fullmatch(sha):
        raise Refused("assembly manifest output.sha256 is malformed")
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        raise Refused("assembly manifest output.size is malformed")
    if not isinstance(root_uuid, str) or not UUID_RE.fullmatch(root_uuid):
        raise Refused("assembly manifest rootfs.uuid is malformed")
    changes = raw.get("changes") if isinstance(raw.get("changes"), dict) else {}
    kernel = changes.get("modules_release")
    if not isinstance(kernel, str) or not kernel:
        layers = raw.get("layers") if isinstance(raw.get("layers"), dict) else {}
        match = re.search(r"Image-([0-9][0-9A-Za-z._+-]*)", str(layers.get("kernel", "")))
        kernel = match.group(1) if match else ""
    if not kernel:
        raise Refused("assembly manifest has no kernel release")
    source = raw.get("source") if isinstance(raw.get("source"), dict) else {}
    return CandidateManifest(
        schema=ASSEMBLY_SCHEMA,
        candidate_id=f"assembly-{sha[:16]}",
        board_model="RDK X5",
        base_os_id="debian",
        base_os_version="12",
        kernel_release=kernel,
        fnos_id="fnos",
        fnos_version=product_version,
        root_uuid=root_uuid,
        image_sha256=sha.lower(),
        image_size_bytes=size,
        source=str(source.get("rootfs_partition") or output.get("path") or "assembly"),
        created_at=str(raw.get("generated_at") or ""),
        # A static all_pass is not a candidate acceptance.  The actual
        # assembly remains unwriteable until a separate reviewed acceptance
        # record is attached.
        accepted=False,
    )



def _normalise_assembly_v2(raw: dict[str, Any], product_version: str | None) -> CandidateManifest:
    candidate = raw.get("candidate") if isinstance(raw.get("candidate"), dict) else {}
    rootfs = raw.get("rootfs") if isinstance(raw.get("rootfs"), dict) else {}
    os_info = raw.get("os") if isinstance(raw.get("os"), dict) else {}
    os_release = os_info.get("os_release") if isinstance(os_info.get("os_release"), dict) else {}
    trim = os_info.get("trim_package") if isinstance(os_info.get("trim_package"), dict) else {}
    sha = candidate.get("sha256")
    size = candidate.get("size")
    root_uuid = rootfs.get("uuid")
    if not isinstance(sha, str) or not SHA_RE.fullmatch(sha):
        raise Refused("assembly v2 candidate.sha256 is malformed")
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        raise Refused("assembly v2 candidate.size is malformed")
    if not isinstance(root_uuid, str) or not UUID_RE.fullmatch(root_uuid):
        raise Refused("assembly v2 rootfs.uuid is malformed")
    base_id = os_release.get("ID")
    base_version = os_release.get("VERSION_ID")
    if not isinstance(base_id, str) or not base_id:
        raise Refused("assembly v2 os.os_release.ID is missing")
    if not isinstance(base_version, str) or not base_version:
        raise Refused("assembly v2 os.os_release.VERSION_ID is missing")
    trim_file = os_info.get("trim_version_file")
    if not isinstance(trim_file, str) or not VERSION_RE.fullmatch(trim_file):
        raise Refused("assembly v2 os.trim_version_file is malformed")
    if trim.get("version") != trim_file:
        raise Refused("assembly v2 os.trim_package.version != trim_version_file")
    if trim.get("status") != "install ok installed":
        raise Refused("assembly v2 trim package is not installed")
    if product_version and product_version != trim_file:
        raise Refused("--product-version conflicts with assembly v2 os.trim_version_file")
    changes = raw.get("changes") if isinstance(raw.get("changes"), dict) else {}
    kernel = changes.get("modules_release")
    if not isinstance(kernel, str) or not kernel:
        # r3 keeps the same verified r2 rootfs and moved the single module
        # release into verification.modules; accept only an unambiguous list.
        verification = raw.get("verification") if isinstance(raw.get("verification"), dict) else {}
        modules = verification.get("modules")
        if isinstance(modules, list) and len(modules) == 1 and isinstance(modules[0], str):
            kernel = modules[0]
    if not isinstance(kernel, str) or not kernel:
        raise Refused("assembly v2 kernel/module release is missing")
    release = raw.get("release") if isinstance(raw.get("release"), str) else "r1"
    return CandidateManifest(
        schema=ASSEMBLY_V2_SCHEMA,
        candidate_id=f"{release}-{sha[:16]}",
        board_model="RDK X5",
        base_os_id=base_id,
        base_os_version=base_version,
        kernel_release=kernel,
        fnos_id="fnos",
        fnos_version=trim_file,
        root_uuid=root_uuid,
        image_sha256=sha.lower(),
        image_size_bytes=size,
        source=str(candidate.get("path") or "assembly-v2"),
        created_at=str(raw.get("generated_at") or ""),
        # Static final-readback is not a write acceptance.
        accepted=False,
    )

def load_candidate(path: Path, *, product_version: str | None = None) -> CandidateManifest:
    path = Path(path).expanduser()
    if path.is_symlink():
        raise Refused("candidate manifest must not be a symlink")
    raw = read_json(path)
    if not isinstance(raw, dict):
        raise Refused("candidate manifest must be an object")
    schema = raw.get("schema")
    if schema == LAB_SCHEMA:
        return _normalise_lab(raw)
    if schema == ASSEMBLY_SCHEMA:
        if not product_version and not (
            isinstance(raw.get("product"), dict)
            and isinstance(raw["product"].get("trim_version"), str)
        ):
            raise Refused(
                "assembly manifest requires an explicit --product-version adapter "
                "(for example 1.2.0701) or product.trim_version; this consumer does "
                "not derive trim identity from Debian os-release"
            )
        return _normalise_assembly(raw, product_version)
    if schema == ASSEMBLY_V2_SCHEMA:
        return _normalise_assembly_v2(raw, product_version)
    raise Refused(
        f"candidate manifest schema must be {LAB_SCHEMA} or {ASSEMBLY_SCHEMA} (got {schema!r})"
    )


def verify_candidate_image(manifest: CandidateManifest, image_path: Path) -> dict[str, Any]:
    image_path = Path(image_path)
    if image_path.is_symlink() or not image_path.is_file():
        raise Refused("candidate image must be an absolute regular non-symlink file")
    h = hashlib.sha256()
    total = 0
    with image_path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
            total += len(chunk)
    if total != manifest.image_size_bytes:
        raise Refused(
            f"candidate image size {total} != manifest {manifest.image_size_bytes}"
        )
    if h.hexdigest() != manifest.image_sha256:
        raise Refused("candidate image SHA-256 does not match the manifest")
    return {"path": str(image_path), "size_bytes": total, "sha256": h.hexdigest()}


def require_candidate_identity(
    manifest: CandidateManifest, identity: RuntimeIdentity
) -> None:
    require_identity(identity, manifest.expectation())
