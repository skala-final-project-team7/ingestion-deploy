#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

echo "==> Running format from $ROOT_DIR"

# Root Java project
if [ -f "./gradlew" ]; then
  if ./gradlew tasks --all | grep -q "spotlessApply"; then
    echo ""
    echo "==> ./gradlew spotlessApply"
    ./gradlew spotlessApply
  else
    echo ""
    echo "==> spotlessApply task not found in root Gradle project. Skipping Java format."
  fi
elif [ -f "./mvnw" ]; then
  echo ""
  echo "==> Maven format is project-specific. Configure spotless:apply or formatter plugin if needed."
fi

# Root Python project (단독 RAG / 단일 모듈 저장소)
if [ -f "./pyproject.toml" ] || [ -f "./requirements.txt" ]; then
  echo ""
  echo "==> root python format"
  if command -v ruff >/dev/null 2>&1; then
    ruff format .
    ruff check . --fix
  elif command -v black >/dev/null 2>&1; then
    black .
  else
    echo "ruff/black not found. Skipping root Python format."
  fi
fi

# Backend Java project
if [ -f "./backend/gradlew" ]; then
  echo ""
  echo "==> backend format"
  if (cd backend && ./gradlew tasks --all | grep -q "spotlessApply"); then
    (cd backend && ./gradlew spotlessApply)
  else
    echo "spotlessApply task not found in backend. Skipping backend Java format."
  fi
elif [ -f "./backend/mvnw" ]; then
  echo ""
  echo "==> Maven backend format is project-specific. Configure spotless:apply or formatter plugin if needed."
fi

# Frontend Node project
if [ -f "./frontend/package.json" ]; then
  echo ""
  echo "==> frontend format"
  if [ -f "./frontend/pnpm-lock.yaml" ]; then
    (cd frontend && pnpm format)
  elif [ -f "./frontend/yarn.lock" ]; then
    (cd frontend && yarn format)
  else
    (cd frontend && npm run format)
  fi
fi

# Python projects
for dir in rag-pipeline ai-agent; do
  if [ -d "./$dir" ] && { [ -f "./$dir/pyproject.toml" ] || [ -f "./$dir/requirements.txt" ]; }; then
    echo ""
    echo "==> $dir format"

    if command -v ruff >/dev/null 2>&1; then
      (cd "$dir" && ruff format .)
      (cd "$dir" && ruff check . --fix)
    elif command -v black >/dev/null 2>&1; then
      (cd "$dir" && black .)
    else
      echo "ruff/black not found. Skipping Python format for $dir."
    fi
  fi
done

echo ""
echo "==> Format completed"
