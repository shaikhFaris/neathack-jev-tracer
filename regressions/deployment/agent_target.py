from jev_tracer.sample_agent import run_sample_agent

def run(url, expected_status, body_contains):
    return run_sample_agent(url, expected_status, body_contains)
