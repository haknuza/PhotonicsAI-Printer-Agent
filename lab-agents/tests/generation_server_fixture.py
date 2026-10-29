"""Browser-test-only provider fixture with REAL OpenSCAD, no cloud calls or keys.

Not a production provider or app mode. Requires explicit temporary job storage.
"""
import argparse
import os
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'lab-agents'))
sys.path.insert(0,str(ROOT/'printing_agent/tests'))
from app import create_app
from printing_agent.agent import PrintingAgent
from printing_agent.generation import GenerationJobs
from printing_agent.openscad import ModelJobs
from test_generation import FixtureProvider, QUESTIONS, READY


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--printer-mode',choices=['mock'],default='mock')
    parser.add_argument('--port',type=int,required=True)
    parser.add_argument('--print-fixture',action='store_true')
    args=parser.parse_args()
    if not os.environ.get('OPENSCAD_JOBS_DIR'):
        parser.error('Use explicit disposable OPENSCAD_JOBS_DIR for this test fixture.')
    source='// Scripted browser-test fixture, not live AI output.\nwidth=24; depth=18; height=6; cube([width,depth,height]);'
    provider=FixtureProvider([QUESTIONS,
        dict(READY,source=source,title='Scripted browser fixture, not live AI'),
        dict(READY,source=source.replace('height=6','height=9'),title='Revised browser fixture, not live AI'),
        dict(READY,source='import("/etc/passwd");')])
    agent=PrintingAgent()
    models=ModelJobs(os.environ['OPENSCAD_JOBS_DIR'])
    generations=GenerationJobs(models,provider)
    prints = None
    if args.print_fixture:
        from printing_agent.print_jobs import PrintJobs
        from test_print_jobs import FixtureTransport
        prints = PrintJobs(models.root / 'prints', models, transport=FixtureTransport())
    try:
        create_app(agent,models=models,generations=generations,prints=prints).run(host='127.0.0.1',port=args.port,threaded=True,debug=False)
    finally:
        if prints:
            prints.close()
        generations.close()
        models.close()
        agent.close()


if __name__=='__main__': main()
