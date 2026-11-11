"""Native Chinese text vs measured CPU/BPU image vectors; no index writes."""
import argparse
import hashlib
import json
from pathlib import Path
import statistics
import time

from .semantic768_backend import MODEL_SHA256, WORKER_SHA256, NATIVE_IMAGE_SHA256, checked_digest
from .semantic768_benchmark import embedding, native_text_identity, process_tree, publish_report, NativeProtocolError


def load_vectors(report_path, query_manifest):
    import numpy as np
    source = json.loads(report_path.read_text())
    if source.get('schema') != 'fnos-native-semantic768-benchmark/v1' or source.get('progress', {}).get('phase') != 'complete':
        raise ValueError('image benchmark is incomplete')
    if source.get('gates', {}).get('preprocessing') is not True or source['gates'].get('features') is not True:
        raise ValueError('image preprocessing/features did not pass')
    if query_manifest.get('schema') != 'fnos-semantic768-chinese-consistency-queries/v1':
        raise ValueError('unsupported query manifest')
    if query_manifest['fixture_manifest_sha256'] != source.get('fixture_manifest_sha256'):
        raise ValueError('queries belong to another image corpus')
    checked_digest(source['fixture_manifest_path'], source['fixture_manifest_sha256'])
    fixtures = json.loads(Path(source['fixture_manifest_path']).read_text())
    metadata = source['vectors']
    expected = {'dimensions': 768, 'native_image_sha256': NATIVE_IMAGE_SHA256,
                'bpu_model_sha256': MODEL_SHA256, 'worker_sha256': WORKER_SHA256}
    if any(metadata.get(key) != value for key, value in expected.items()):
        raise ValueError('image vectors use an unreviewed model family')
    path = (report_path.parent / metadata['file']).resolve()
    if path.parent != report_path.parent.resolve():
        raise ValueError('vector artifact must be adjacent to its benchmark report')
    checked_digest(path, metadata['sha256'])
    images = metadata['images']
    if len(images) != 28 or len({row['path'] for row in images}) != 28 or images != fixtures['images']:
        raise ValueError('all 28 measured images are required')
    with np.load(path, allow_pickle=False) as archive:
        if set(archive.files) != {'cpu', 'bpu'}:
            raise ValueError('unexpected vector archive')
        cpu, bpu = archive['cpu'].astype(np.float64), archive['bpu'].astype(np.float64)
    for vectors in (cpu, bpu):
        if vectors.shape != (28, 768) or not np.isfinite(vectors).all() or np.max(np.abs(np.linalg.norm(vectors, axis=1) - 1)) > 1e-5:
            raise ValueError('invalid measured vectors')
    cosines = np.sum(cpu * bpu, axis=1)
    retrieval = float(np.mean(np.argmax(bpu @ cpu.T, axis=1) == np.arange(28)))
    if cosines.min() < .99 or cosines.mean() < .995 or retrieval < .95:
        raise ValueError('vector artifact does not preserve image feature accuracy')
    queries = query_manifest.get('queries', [])
    if not queries or any(not isinstance(row.get('text'), str) or not row['text'].strip() or 'expected_images' in row for row in queries):
        raise ValueError('this unlabelled comparison requires nonempty text without relevance labels')
    if len({row['text'] for row in queries}) != len(queries):
        raise ValueError('duplicate queries')
    return source, images, cpu, bpu


def main():
    import numpy as np
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('benchmark-report', 'queries-manifest', 'text-socket', 'text-receipt', 'report'):
        parser.add_argument('--' + name, required=True, type=Path)
    args = parser.parse_args()
    query_manifest = json.loads(args.queries_manifest.read_text())
    source, images, cpu, bpu = load_vectors(args.benchmark_report, query_manifest)
    identity = native_text_identity(args.text_socket, args.text_receipt)
    results = []
    report = {'schema': 'fnos-semantic768-native-chinese-consistency/v1', 'status': 'running',
              'scope': 'unlabelled native CPU/BPU ranking consistency only; no retrieval accuracy or product acceptance claim',
              'benchmark_report_sha256': hashlib.sha256(args.benchmark_report.read_bytes()).hexdigest(),
              'queries_manifest_sha256': hashlib.sha256(args.queries_manifest.read_bytes()).hexdigest(),
              'vector_artifact_sha256': source['vectors']['sha256'], 'native_text_identity': identity,
              'relevance_accuracy': 'not_measured_no_human_labels', 'queries': results,
              'progress': {'completed': 0, 'total': len(query_manifest['queries'])}}
    publish_report(args.report, report)
    try:
        for item in query_manifest['queries']:
            if process_tree(identity['pid'])[0]['start_ticks'] != identity['process']['start_ticks']:
                raise NativeProtocolError('text process restarted during comparison')
            start = time.perf_counter()
            text = embedding(args.text_socket, 'inferTxt2Vec', {'txt': item['text']}, expected_pid=identity['pid'])
            elapsed = time.perf_counter() - start
            cpu_scores, bpu_scores = cpu @ text, bpu @ text
            old_rank, new_rank = np.argsort(-cpu_scores), np.argsort(-bpu_scores)
            old_top, new_top = int(old_rank[0]), int(new_rank[0])
            results.append({'text': item['text'], 'text_rpc_s': elapsed,
                            'cpu_top1': images[old_top]['path'], 'bpu_top1': images[new_top]['path'],
                            'top1_agrees': old_top == new_top,
                            'top5_overlap': len(set(old_rank[:5]) & set(new_rank[:5])) / 5,
                            'max_score_change': float(np.max(np.abs(cpu_scores - bpu_scores))),
                            'cpu_top5': [{'image': images[int(index)]['path'], 'score': float(cpu_scores[index])} for index in old_rank[:5]],
                            'bpu_top5': [{'image': images[int(index)]['path'], 'score': float(bpu_scores[index])} for index in new_rank[:5]]})
            report['progress']['completed'] = len(results)
            publish_report(args.report, report)
            print(json.dumps({'completed': len(results), 'total': len(query_manifest['queries']), 'text': item['text'], 'top1_agrees': old_top == new_top}, ensure_ascii=False), flush=True)
        current = native_text_identity(args.text_socket, args.text_receipt)
        for key in ('pid', 'executable_sha256', 'model_sha256', 'tokenizer_sha256'):
            if current[key] != identity[key]:
                raise NativeProtocolError('native text identity changed')
        if current['process']['start_ticks'] != identity['process']['start_ticks']:
            raise NativeProtocolError('text process restarted')
        report.update(top1_agreement=statistics.mean(row['top1_agrees'] for row in results),
                      mean_top5_overlap=statistics.mean(row['top5_overlap'] for row in results),
                      gate={'top1_agreement_min': .95})
        report['status'] = 'native_chinese_result_consistency_pass' if report['top1_agreement'] >= .95 else 'native_chinese_result_consistency_fail'
    except BaseException as error:
        report.update(status='failed', error=repr(error))
        raise
    finally:
        publish_report(args.report, report)
    return int(report['status'] != 'native_chinese_result_consistency_pass')


if __name__ == '__main__':
    raise SystemExit(main())
