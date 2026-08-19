// Jenkins port of .github/workflows/docker-publish.yml (tests + image publish).
// 3.11 is what the container ships (Dockerfile: python:3.11-slim); 3.14 is the
// development host. Every pin in requirements.txt has to install on both.
// Both interpreters are pre-installed in the agent image via uv.
// Publish runs only on main and pushes to GHCR with the `github-pat` credential.
pipeline {
  agent { label 'docker' }

  options {
    disableConcurrentBuilds(abortPrevious: true)
  }

  environment {
    REGISTRY = 'ghcr.io'
    IMAGE = 'ghcr.io/d7eeem/feather'
    BOT_IMAGE = 'ghcr.io/d7eeem/feather-bot'
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

    stage('Build & push image') {
      when { branch 'main' }
      steps {
        withCredentials([usernamePassword(credentialsId: 'github-pat',
            usernameVariable: 'REG_USER', passwordVariable: 'REG_TOKEN')]) {
          sh 'echo "$REG_TOKEN" | docker login "$REGISTRY" -u "$REG_USER" --password-stdin'
        }
        sh '''
          set -eu
          SHORT=$(git rev-parse --short HEAD)
          docker build -t "$IMAGE:main" -t "$IMAGE:latest" -t "$IMAGE:sha-$SHORT" .
          docker push "$IMAGE:main"
          docker push "$IMAGE:latest"
          docker push "$IMAGE:sha-$SHORT"
        '''
      }
    }

    stage('Smoke-test image') {
      when { branch 'main' }
      steps {
        // Same assertions as the old workflow: the release importer must run,
        // the app must refuse to boot without ADMIN_PASSWORD, and the three
        // endpoints must answer — curl from INSIDE the container (rootless/
        // containerized Docker doesn't reliably expose published ports).
        sh '''
          set -eu
          OUT="$(docker run --rm "$IMAGE:main" python scripts/release_source_ingest.py --help)"
          echo "$OUT" | grep -q -- "--apply" \
            || { echo "FAIL: release importer --help did not mention --apply"; exit 1; }
          echo "ok: release importer --help works and mentions --apply"
          if docker run --rm -e DATA_DIR=/tmp/feather-data "$IMAGE:main" python -c "import app" 2>/dev/null; then
            echo "FAIL: image booted without ADMIN_PASSWORD"; exit 1
          fi
          echo "ok: refuses to boot without ADMIN_PASSWORD"
          docker rm -f feather-smoke 2>/dev/null || true
          docker run -d --rm --name feather-smoke \
            -e DATA_DIR=/tmp/feather-data \
            -e ADMIN_PASSWORD=ci-smoke-test-not-a-real-password "$IMAGE:main"
          for i in $(seq 1 30); do
            if docker exec feather-smoke curl -fsS -o /dev/null http://localhost:5000/source.json; then break; fi
            sleep 1
          done
          docker exec feather-smoke curl -fsS -o /dev/null -w 'source.json  %{http_code} %{content_type}\\n' \
            http://localhost:5000/source.json
          docker exec feather-smoke curl -fsS -o /dev/null -w 'qr           %{http_code} %{content_type}\\n' \
            http://localhost:5000/qr
          docker exec feather-smoke curl -fsS -o /dev/null -w 'index        %{http_code}\\n' \
            http://localhost:5000/
          docker logs feather-smoke
          docker stop feather-smoke
        '''
      }
    }

    stage('Build & push bot image') {
      when { branch 'main' }
      steps {
        sh '''
          set -eu
          SHORT=$(git rev-parse --short HEAD)
          docker build -f Dockerfile.bot -t "$BOT_IMAGE:main" -t "$BOT_IMAGE:latest" -t "$BOT_IMAGE:sha-$SHORT" .
          docker push "$BOT_IMAGE:main"
          docker push "$BOT_IMAGE:latest"
          docker push "$BOT_IMAGE:sha-$SHORT"
        '''
      }
    }

    stage('Smoke-test bot image') {
      when { branch 'main' }
      steps {
        // The worker must refuse to run unconfigured, naming what is missing.
        sh '''
          set -eu
          OUT="$(docker run --rm "$BOT_IMAGE:main" 2>&1 || true)"
          echo "$OUT"
          echo "$OUT" | grep -q "TELEGRAM_BOT_TOKEN" \
            || { echo "FAIL: did not name the missing variables"; exit 1; }
          if docker run --rm "$BOT_IMAGE:main" >/dev/null 2>&1; then
            echo "FAIL: bot started with no configuration"; exit 1
          fi
          echo "ok: refuses to run unconfigured, names the missing variables"
        '''
      }
    }
  }

  post {
    always {
      sh 'docker logout "$REGISTRY" || true'
    }
  }
}
