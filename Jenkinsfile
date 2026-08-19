// Jenkins port of the `test` job in .github/workflows/docker-publish.yml.
// Image build/publish to GHCR stays on GitHub Actions (self-hosted runner).
// 3.11 is what the container ships (Dockerfile: python:3.11-slim); 3.14 is the
// development host. Every pin in requirements.txt has to install on both.
// Both interpreters are pre-installed in the agent image via uv.
pipeline {
  agent { label 'docker' }

  options {
    disableConcurrentBuilds(abortPrevious: true)
  }

  stages {
    stage('Tests (Python 3.11)') {
      steps {
        sh '''
          uv venv --python 3.11 .venv311
          . .venv311/bin/activate
          uv pip install -r requirements.txt -r requirements-dev.txt
          python -m pytest tests/ -q
        '''
      }
    }

    stage('Tests (Python 3.14)') {
      steps {
        sh '''
          uv venv --python 3.14 .venv314
          . .venv314/bin/activate
          uv pip install -r requirements.txt -r requirements-dev.txt
          python -m pytest tests/ -q
        '''
      }
    }

    stage('requests pin drift check') {
      steps {
        sh '''
          set -euo pipefail
          REQ="$(grep -oP '^requests==\\K[0-9.]+' requirements.txt)"
          BOT="$(grep -oP 'requests==\\K[0-9.]+' Dockerfile.bot)"
          echo "requirements.txt: $REQ   Dockerfile.bot: $BOT"
          [ "$REQ" = "$BOT" ] || { echo "FAIL: requests pins have drifted"; exit 1; }
        '''
      }
    }
  }
}
