# Root Directory Files Audit

**Date**: April 16, 2026  
**Purpose**: Identify and categorize all files in project root directory

---

## 📊 Summary

**Total Files**: 24 files in root directory  
**Total Size**: ~20 MB (excluding large archives)

### File Categories
- ✅ **Keep (Essential)**: 9 files
- 🗄️ **Archive**: 8 files (working files, logs, dumps)
- 🗑️ **Delete**: 7 files (temporary, zone identifiers, old archives)

---

## ✅ Essential Files (Keep in Root)

### 1. **docker-compose.yml** (5.7 KB)
- **Purpose**: Docker orchestration for development/staging
- **Status**: ✅ Essential - Keep
- **Used by**: Development, deployment
- **Action**: None

### 2. **package.json** (95 bytes)
- **Purpose**: Monorepo workspace configuration
- **Content**: Defines frontend workspace
- **Status**: ✅ Essential - Keep
- **Action**: None

### 3. **package-lock.json** (129 KB)
- **Purpose**: NPM dependency lock file
- **Status**: ✅ Essential - Keep
- **Action**: None

### 4. **prometheus.yml** (364 bytes)
- **Purpose**: Prometheus monitoring configuration
- **Status**: ✅ Essential - Keep
- **Action**: None

### 5. **DOCUMENTATION_INDEX.md** (9.7 KB)
- **Purpose**: Master documentation index
- **Status**: ✅ Essential - Keep
- **Created**: April 16, 2026
- **Action**: None

### 6. **DOCUMENTATION_ORGANIZATION_SUMMARY.md** (7.4 KB)
- **Purpose**: Documentation organization report
- **Status**: ✅ Essential - Keep
- **Created**: April 16, 2026
- **Action**: None

---

## 🔧 Development Scripts (Keep in Root)

### 7. **start-dev.sh** (4.1 KB)
- **Purpose**: Start development environment (full setup)
- **Status**: ✅ Essential - Keep
- **Features**: Database, Redis, backend, frontend, worker
- **Action**: None

### 8. **start-dev-simple.sh** (1.3 KB)
- **Purpose**: Simple development startup (minimal)
- **Status**: ✅ Essential - Keep
- **Action**: None

### 9. **stop-dev.sh** (557 bytes)
- **Purpose**: Stop development environment
- **Status**: ✅ Essential - Keep
- **Action**: None

### 10. **health-check.sh** (1.5 KB)
- **Purpose**: System health check script
- **Status**: ✅ Essential - Keep
- **Action**: None

### 11. **quick-ref.sh** (2.9 KB)
- **Purpose**: Quick reference commands
- **Status**: ✅ Essential - Keep
- **Action**: None

### 12. **test-auth.sh** (1.6 KB)
- **Purpose**: Test authentication endpoints
- **Status**: ✅ Useful - Keep
- **Action**: Consider moving to `scripts/testing/`

### 13. **test-upstox.sh** (2.0 KB)
- **Purpose**: Test Upstox integration
- **Status**: ✅ Useful - Keep
- **Action**: Consider moving to `scripts/testing/`

---

## 🗄️ Archive (Move to Archive Directory)

### 14. **~final_start_check.md** (608 KB)
- **Purpose**: Conversation history/working file
- **Status**: 🗄️ Archive
- **Content**: Chat conversation transcript
- **Action**: Move to `archive/conversation_history/`
- **Reason**: Historical record, not active documentation

### 15. **~back_to_main_tasks.json** (2.8 MB)
- **Purpose**: Working file/conversation state
- **Status**: 🗄️ Archive
- **Action**: Move to `archive/conversation_history/`
- **Reason**: Historical record, large file

### 16. **~working_on_ml.json** (1.3 MB)
- **Purpose**: Working file/conversation state
- **Status**: 🗄️ Archive
- **Action**: Move to `archive/conversation_history/`
- **Reason**: Historical record, large file

### 17. **dump_ML.txt** (1.3 MB)
- **Purpose**: Code dump/export
- **Status**: 🗄️ Archive
- **Action**: Move to `archive/code_dumps/`
- **Reason**: Historical snapshot, not needed for operation

### 18. **results_for_WS_for_ml.txt** (33 KB)
- **Purpose**: Web search results for ML research
- **Status**: 🗄️ Archive
- **Action**: Move to `archive/research/`
- **Reason**: Historical research, not active

### 19. **web_search_for_ml.txt** (8.8 KB)
- **Purpose**: Web search queries/results
- **Status**: 🗄️ Archive
- **Action**: Move to `archive/research/`
- **Reason**: Historical research, not active

### 20. **generate_dump.sh** (3.0 KB)
- **Purpose**: Script to generate code dumps
- **Status**: 🗄️ Archive or Keep
- **Action**: Move to `scripts/utilities/` or archive
- **Reason**: Utility script, may be useful for future dumps

---

## 🗑️ Delete (Safe to Remove)

### 21. **cortex_v4_w_aimsvc-master.zip** (13 MB)
- **Purpose**: Original source archive (pre-merge)
- **Status**: 🗑️ Delete
- **Reason**: Already extracted to `cortex_v4_w_aimsvc-master/` directory
- **Action**: Delete (source is in directory)

### 22. **cortex_v4_w_aimsvc-master.zip:Zone.Identifier** (25 bytes)
- **Purpose**: Windows zone identifier (download marker)
- **Status**: 🗑️ Delete
- **Reason**: Windows metadata, not needed on Linux
- **Action**: Delete

### 23. **cortex_merge_plan.md:Zone.Identifier** (25 bytes)
- **Purpose**: Windows zone identifier
- **Status**: 🗑️ Delete
- **Reason**: Windows metadata, not needed
- **Action**: Delete

### 24. **cuda-keyring_1.0-1_all.deb** (4.3 KB)
- **Purpose**: CUDA keyring package
- **Status**: 🗑️ Delete or Move
- **Reason**: Installation artifact, not needed in repo
- **Action**: Delete (can be re-downloaded if needed)

---

## 📋 Recommended Actions

### Immediate Actions

#### 1. Create Archive Directory
```bash
mkdir -p archive/{conversation_history,code_dumps,research}
```

#### 2. Move Archive Files
```bash
# Conversation history
mv ~final_start_check.md archive/conversation_history/
mv ~back_to_main_tasks.json archive/conversation_history/
mv ~working_on_ml.json archive/conversation_history/

# Code dumps
mv dump_ML.txt archive/code_dumps/

# Research
mv results_for_WS_for_ml.txt archive/research/
mv web_search_for_ml.txt archive/research/
```

#### 3. Delete Unnecessary Files
```bash
# Delete zone identifiers
rm cortex_v4_w_aimsvc-master.zip:Zone.Identifier
rm cortex_merge_plan.md:Zone.Identifier

# Delete archive (source already extracted)
rm cortex_v4_w_aimsvc-master.zip

# Delete installation artifact
rm cuda-keyring_1.0-1_all.deb
```

#### 4. Move Test Scripts (Optional)
```bash
# Move to scripts/testing/
mv test-auth.sh scripts/testing/
mv test-upstox.sh scripts/testing/
```

#### 5. Move Utility Script (Optional)
```bash
# Move to scripts/utilities/
mv generate_dump.sh scripts/utilities/
```

---

## 📊 After Cleanup

### Root Directory Will Contain:
```
/home/preet/code/Cortex_Merge_AI-ML/
├── docker-compose.yml                          # Docker orchestration
├── package.json                                # Monorepo config
├── package-lock.json                           # NPM lock file
├── prometheus.yml                              # Monitoring config
├── DOCUMENTATION_INDEX.md                      # Documentation index
├── DOCUMENTATION_ORGANIZATION_SUMMARY.md       # Organization report
├── start-dev.sh                                # Start dev environment
├── start-dev-simple.sh                         # Simple start
├── stop-dev.sh                                 # Stop dev environment
├── health-check.sh                             # Health check
├── quick-ref.sh                                # Quick reference
├── test-auth.sh                                # Auth tests (optional)
├── test-upstox.sh                              # Upstox tests (optional)
├── generate_dump.sh                            # Dump generator (optional)
├── backend/                                    # Backend code
├── frontend/                                   # Frontend code
├── documentation/                              # Documentation
├── scripts/                                    # Scripts
├── archive/                                    # Archived files (new)
└── ...
```

### Benefits
- ✅ Clean root directory (14 files vs 24)
- ✅ Clear separation of active vs archived files
- ✅ Easier navigation
- ✅ Reduced clutter
- ✅ Preserved history in archive

---

## 🔍 File Purpose Summary

### Configuration Files (4)
- `docker-compose.yml` - Container orchestration
- `package.json` - Monorepo workspace
- `package-lock.json` - NPM dependencies
- `prometheus.yml` - Monitoring config

### Documentation (2)
- `DOCUMENTATION_INDEX.md` - Master index
- `DOCUMENTATION_ORGANIZATION_SUMMARY.md` - Organization report

### Development Scripts (5)
- `start-dev.sh` - Full dev startup
- `start-dev-simple.sh` - Simple startup
- `stop-dev.sh` - Stop services
- `health-check.sh` - Health checks
- `quick-ref.sh` - Quick commands

### Test Scripts (2)
- `test-auth.sh` - Auth testing
- `test-upstox.sh` - Upstox testing

### Utility Scripts (1)
- `generate_dump.sh` - Code dump generator

### Archive Files (8)
- Conversation history (3 files, 4.9 MB)
- Code dumps (1 file, 1.3 MB)
- Research notes (2 files, 42 KB)

### Delete Files (4)
- Zone identifiers (2 files, 50 bytes)
- Old archive (1 file, 13 MB)
- Installation artifact (1 file, 4.3 KB)

---

## ✅ Cleanup Commands

### Complete Cleanup Script
```bash
#!/bin/bash
# Root directory cleanup script

cd /home/preet/code/Cortex_Merge_AI-ML

# Create archive directories
mkdir -p archive/{conversation_history,code_dumps,research}

# Move conversation history
mv ~final_start_check.md archive/conversation_history/ 2>/dev/null
mv ~back_to_main_tasks.json archive/conversation_history/ 2>/dev/null
mv ~working_on_ml.json archive/conversation_history/ 2>/dev/null

# Move code dumps
mv dump_ML.txt archive/code_dumps/ 2>/dev/null

# Move research
mv results_for_WS_for_ml.txt archive/research/ 2>/dev/null
mv web_search_for_ml.txt archive/research/ 2>/dev/null

# Delete unnecessary files
rm -f cortex_v4_w_aimsvc-master.zip:Zone.Identifier
rm -f cortex_merge_plan.md:Zone.Identifier
rm -f cortex_v4_w_aimsvc-master.zip
rm -f cuda-keyring_1.0-1_all.deb

# Optional: Move test scripts
# mv test-auth.sh scripts/testing/
# mv test-upstox.sh scripts/testing/

# Optional: Move utility script
# mv generate_dump.sh scripts/utilities/

echo "✅ Root directory cleanup complete"
echo "📊 Files moved to archive/"
echo "🗑️ Unnecessary files deleted"
```

---

## 📈 Impact Analysis

### Before Cleanup
- **Total Files**: 24
- **Total Size**: ~20 MB
- **Clutter Level**: High
- **Navigation**: Difficult

### After Cleanup
- **Total Files**: 11-14 (depending on optional moves)
- **Total Size**: ~150 KB (excluding node_modules)
- **Clutter Level**: Low
- **Navigation**: Easy

### Space Saved
- **Archived**: ~6.2 MB (preserved)
- **Deleted**: ~13 MB (can be re-downloaded)
- **Total Cleanup**: ~19 MB

---

## 🚨 Important Notes

### Before Deleting
1. ✅ Verify `cortex_v4_w_aimsvc-master/` directory exists
2. ✅ Confirm conversation history is backed up elsewhere
3. ✅ Check if any scripts reference deleted files

### Archive Retention
- Keep archive for 6-12 months
- Review periodically for deletion
- Compress large files if needed

### Git Ignore
Add to `.gitignore`:
```
# Archives
archive/

# Zone identifiers
*.Zone.Identifier

# Temporary files
~*.md
~*.json

# Dumps
dump_*.txt
```

---

**Audit Complete**: ✅  
**Files Analyzed**: 24  
**Recommended Actions**: Archive 8, Delete 4, Keep 12  
**Estimated Cleanup**: ~19 MB
