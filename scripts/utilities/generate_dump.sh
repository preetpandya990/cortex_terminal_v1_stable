#!/bin/bash

OUTPUT_FILE="/home/preet/code/Cortex_Merge_AI-ML/dump.txt"
PROJECT_ROOT="/home/preet/code/Cortex_Merge_AI-ML"

# Clear output file
> "$OUTPUT_FILE"

echo "=== CODEBASE DUMP ===" >> "$OUTPUT_FILE"
echo "Generated: $(date)" >> "$OUTPUT_FILE"
echo "" >> "$OUTPUT_FILE"

# Generate tree structure (excluding specific patterns)
echo "=== FILE STRUCTURE ===" >> "$OUTPUT_FILE"
echo "" >> "$OUTPUT_FILE"
cd "$PROJECT_ROOT"
tree -I "node_modules|__pycache__|*.pyc|.git|dist|build|.next|.cache|venv|env|*.egg-info|coverage|.pytest_cache|code_dump_chunks|.venv|.axon|.kiro|.codex|.vscode|documentation|wsl.localhost|.hypothesis|docs|agent.md|dump.txt" -a --charset ascii >> "$OUTPUT_FILE" 2>&1
echo "" >> "$OUTPUT_FILE"
echo "" >> "$OUTPUT_FILE"

# Function to process files
process_files() {
    local dir="$1"
    
    find "$dir" -type f \
        -not -path "*/node_modules/*" \
        -not -path "*/__pycache__/*" \
        -not -path "*/.git/*" \
        -not -path "*/dist/*" \
        -not -path "*/build/*" \
        -not -path "*/.next/*" \
        -not -path "*/.cache/*" \
        -not -path "*/venv/*" \
        -not -path "*/env/*" \
        -not -path "*/.venv/*" \
        -not -path "*/code_dump_chunks/*" \
        -not -path "*/.pytest_cache/*" \
        -not -path "*/.axon/*" \
        -not -path "*/.kiro/*" \
        -not -path "*/.codex/*" \
        -not -path "*/.vscode/*" \
        -not -path "*/documentation/*" \
        -not -path "*/wsl.localhost/*" \
        -not -path "*/.hypothesis/*" \
        -not -path "*/docs/*" \
        -not -name "*.pyc" \
        -not -name "*.so" \
        -not -name "*.bc" \
        -not -name "*.a" \
        -not -name "*.o" \
        -not -name "*.whl" \
        -not -name "*.egg-info" \
        -not -name "package-lock.json" \
        -not -name "*.log" \
        -not -name "dump.txt" \
        -not -name "agent.md" \
        | sort | while read -r file; do
        
        # Check if file is text
        if file "$file" | grep -q "text"; then
            echo "=== FILE: ${file#$PROJECT_ROOT/} ===" >> "$OUTPUT_FILE"
            echo "" >> "$OUTPUT_FILE"
            cat "$file" >> "$OUTPUT_FILE" 2>/dev/null || echo "[Error reading file]" >> "$OUTPUT_FILE"
            echo "" >> "$OUTPUT_FILE"
            echo "" >> "$OUTPUT_FILE"
        fi
    done
}

echo "=== SOURCE CODE ===" >> "$OUTPUT_FILE"
echo "" >> "$OUTPUT_FILE"

# Process main directories
for dir in backend frontend scripts; do
    if [ -d "$PROJECT_ROOT/$dir" ]; then
        process_files "$PROJECT_ROOT/$dir"
    fi
done

# Process root config files (excluding agent.md)
for file in docker-compose.yml .gitignore package.json .env.example; do
    if [ -f "$PROJECT_ROOT/$file" ]; then
        echo "=== FILE: $file ===" >> "$OUTPUT_FILE"
        echo "" >> "$OUTPUT_FILE"
        cat "$PROJECT_ROOT/$file" >> "$OUTPUT_FILE" 2>/dev/null
        echo "" >> "$OUTPUT_FILE"
        echo "" >> "$OUTPUT_FILE"
    fi
done

echo "Dump complete: $OUTPUT_FILE"
