# Root Scripts Directory

Quick access scripts for Cortex AI development and operations.

---

## 📁 Directory Structure

```
root_scripts/
├── dev/                    # Development scripts
│   ├── start-dev.sh        # Start full dev environment (tmux)
│   ├── start-dev-simple.sh # Start dev environment (gnome-terminal)
│   └── stop-dev.sh         # Stop all services
├── ops/                    # Operations scripts
│   ├── health-check.sh     # System health check
│   └── quick-ref.sh        # Quick reference commands
└── testing/                # Testing scripts
    ├── test-auth.sh        # Test authentication flow
    └── test-upstox.sh      # Test Upstox integration
```

---

## 🚀 Quick Start

### Development

**Start development environment (full):**
```bash
./root_scripts/dev/start-dev.sh
```

**Start development environment (simple):**
```bash
./root_scripts/dev/start-dev-simple.sh
```

**Stop all services:**
```bash
./root_scripts/dev/stop-dev.sh
```

### Operations

**Check system health:**
```bash
./root_scripts/ops/health-check.sh
```

**View quick reference:**
```bash
./root_scripts/ops/quick-ref.sh
```

### Testing

**Test authentication:**
```bash
./root_scripts/testing/test-auth.sh
```

**Test Upstox integration:**
```bash
./root_scripts/testing/test-upstox.sh
```

---

## 📝 Script Descriptions

### Development Scripts (`dev/`)

#### start-dev.sh
- **Purpose**: Start full development environment with tmux
- **Features**: Multi-pane terminal, live logs, color-coded output
- **Requirements**: tmux, docker-compose
- **Services**: PostgreSQL, Redis, Backend, Frontend, Worker

#### start-dev-simple.sh
- **Purpose**: Simple development startup without tmux
- **Features**: Separate terminal windows
- **Requirements**: gnome-terminal, docker-compose
- **Services**: Backend, Frontend

#### stop-dev.sh
- **Purpose**: Stop all development services
- **Features**: Graceful shutdown, cleanup
- **Actions**: Stops Docker Compose, kills tmux sessions, cleans logs

---

### Operations Scripts (`ops/`)

#### health-check.sh
- **Purpose**: Check system health
- **Checks**: Backend API, Frontend, Database, Redis
- **Output**: Color-coded status (✅ healthy, ❌ unhealthy)

#### quick-ref.sh
- **Purpose**: Display quick reference commands
- **Sections**: Docker, Database, Testing, Deployment
- **Usage**: Quick command lookup

---

### Testing Scripts (`testing/`)

#### test-auth.sh
- **Purpose**: Test authentication endpoints
- **Tests**: Dev login, JWT tokens, refresh, RBAC
- **Output**: Test results with status codes

#### test-upstox.sh
- **Purpose**: Test Upstox API integration
- **Tests**: Auth, market data, live prices, search
- **Output**: API responses and validation

---

## 🔧 Aliases (Optional)

Add to your `~/.bashrc` or `~/.zshrc`:

```bash
# Cortex AI aliases
alias cortex-start='./root_scripts/dev/start-dev.sh'
alias cortex-stop='./root_scripts/dev/stop-dev.sh'
alias cortex-health='./root_scripts/ops/health-check.sh'
alias cortex-ref='./root_scripts/ops/quick-ref.sh'
alias cortex-test-auth='./root_scripts/testing/test-auth.sh'
alias cortex-test-upstox='./root_scripts/testing/test-upstox.sh'
```

Then reload: `source ~/.bashrc`

---

## 📊 Script Summary

| Category | Scripts | Purpose |
|----------|---------|---------|
| Development | 3 | Start/stop dev environment |
| Operations | 2 | Health checks, reference |
| Testing | 2 | Test auth and integrations |
| **Total** | **7** | Complete dev workflow |

---

## 🎯 Usage Patterns

### Daily Development
```bash
# Morning: Start environment
./root_scripts/dev/start-dev.sh

# During day: Check health
./root_scripts/ops/health-check.sh

# Evening: Stop environment
./root_scripts/dev/stop-dev.sh
```

### Testing Workflow
```bash
# Test authentication
./root_scripts/testing/test-auth.sh

# Test Upstox integration
./root_scripts/testing/test-upstox.sh
```

### Quick Reference
```bash
# View common commands
./root_scripts/ops/quick-ref.sh
```

---

## 📝 Notes

- All scripts have executable permissions
- Scripts are documented with headers
- Color-coded output for better readability
- Error handling included
- Safe to run multiple times

---

**Last Updated**: April 16, 2026  
**Total Scripts**: 7  
**Organization**: 3 categories (dev, ops, testing)
