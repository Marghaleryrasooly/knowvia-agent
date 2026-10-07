"""Isolated process memory probe; no provider API calls or real application data."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile


def memory() -> dict[str, int]:
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("faults", wintypes.DWORD)] + [
                (name, ctypes.c_size_t) for name in (
                    "peak", "working", "pool_peak", "pool", "nonpool_peak",
                    "nonpool", "pagefile", "pagefile_peak")]
        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        kernel = ctypes.WinDLL("kernel32")
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        psapi = ctypes.WinDLL("psapi")
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        if not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            raise OSError("Could not measure process memory")
        return {"rss_bytes": counters.working, "peak_rss_bytes": counters.peak}
    import resource
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    scale = 1 if sys.platform == "darwin" else 1024
    result = {"peak_rss_bytes": peak * scale}
    if Path("/proc/self/statm").exists():
        result["rss_bytes"] = int(Path("/proc/self/statm").read_text().split()[1]) * os.sysconf("SC_PAGE_SIZE")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    def report(stage):
        print(json.dumps({"stage": stage, **memory()}), flush=True)
    report("process_start")
    from knowvia.deployment import ApplicationEnvironment, DeploymentConfig
    from knowvia.runtime_paths import RuntimePaths
    from knowvia.web import create_web_app
    with tempfile.TemporaryDirectory() as root:
        create_web_app(deployment_config=DeploymentConfig(environment=ApplicationEnvironment.TEST),
                       runtime_paths=RuntimePaths(Path(root)))
        report("web_application_created")
        from knowvia.embeddings import OnnxEmbeddingProvider
        from knowvia.accounts import Argon2idPasswordHashService
        from knowvia.retrieval import Retriever
        from knowvia.vector_store import JsonVectorStore
        provider = OnnxEmbeddingProvider(args.directory)
        report("onnx_initialized")
        provider.embed(["How many vacation days do I get?"])
        report("normal_query")
        provider.embed(["VPN " * 256])
        report("near_256_tokens")
        passwords = Argon2idPasswordHashService()
        password_hash = passwords.hash("temporary memory probe password")
        assert passwords.verify(password_hash, "temporary memory probe password")
        report("password_hash_and_verify")
        from knowvia.deployment_bootstrap import DeploymentResources
        Retriever(provider, JsonVectorStore(DeploymentResources.from_environment().bundled_index)).search("VPN troubleshooting")
        report("scoring_request")
        print(json.dumps({"torch_loaded": "torch" in sys.modules,
                          "sentence_transformers_loaded": "sentence_transformers" in sys.modules}))


if __name__ == "__main__":
    main()
