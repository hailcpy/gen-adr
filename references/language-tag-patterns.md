# Language Tag Patterns Reference

How to place `@ADR` inline tags correctly across different languages and file types.
The general format is always:

```
@ADR-<NNN>-<slug>: <one-line summary> — see docs/decisions/<NNN>-<slug>.md
```

---

## Python

**Function:**
```python
# @ADR-0003-async-queue: switched from sync to async event processing — see docs/decisions/0003-async-queue.md
async def process_events(self, batch: list[Event]) -> None:
```

**Class:**
```python
# @ADR-0007-redis-cache: Redis chosen over in-memory cache — see docs/decisions/0007-redis-cache.md
class CacheManager:
```

**Module-scope (cross-cutting decision):** place at the top of the file, after imports:
```python
import redis
from typing import Optional

# @ADR-0011-hexagonal-arch: ports-and-adapters pattern adopted for this module — see docs/decisions/0011-hexagonal-arch.md
```

**What to skip:**
- `@dataclass` fields — tag the class, not individual fields
- `@property` methods — tag the class instead
- `__init__` — tag the class instead, unless the decision is specifically about initialization

---

## TypeScript / JavaScript

**Function:**
```typescript
// @ADR-0004-grpc-transport: gRPC adopted over REST for inter-service calls — see docs/decisions/0004-grpc-transport.md
export async function createServiceClient(addr: string): Promise<ServiceClient> {
```

**Class (use JSDoc block):**
```typescript
/** @ADR-0007-redis-cache: Redis chosen over in-memory cache — see docs/decisions/0007-redis-cache.md */
export class CacheManager {
```

**Interface:**
```typescript
// @ADR-0009-event-contract: CloudEvents spec adopted for event envelope — see docs/decisions/0009-event-contract.md
export interface DomainEvent {
```

**Arrow function exported as const:**
```typescript
// @ADR-0005-date-fns: date-fns replaces moment.js — see docs/decisions/0005-date-fns.md
export const formatDate = (date: Date, fmt: string): string => {
```

**What to skip:**
- Auto-generated files (`*.generated.ts`, `*.pb.ts`, `graphql.tsx`) — don't tag, they'll be overwritten
- `index.ts` barrel files — tag the actual implementation file instead
- Test files (`*.test.ts`, `*.spec.ts`)

---

## Go

**Function:**
```go
// @ADR-0004-grpc-transport: gRPC adopted over REST for inter-service comms — see docs/decisions/0004-grpc-transport.md
func NewServiceClient(addr string) (*ServiceClient, error) {
```

**Struct:**
```go
// @ADR-0007-redis-cache: Redis chosen over in-memory cache — see docs/decisions/0007-redis-cache.md
type CacheManager struct {
```

**Interface:**
```go
// @ADR-0009-event-contract: CloudEvents spec adopted for event envelope — see docs/decisions/0009-event-contract.md
type EventPublisher interface {
```

**Method on a struct:** tag the struct, not every method. Only tag a method if the decision applies specifically to that method and not the whole type.

**Package-level (cross-cutting):** place after the `package` declaration and before imports:
```go
package payments

// @ADR-0011-hexagonal-arch: ports-and-adapters pattern adopted for this package — see docs/decisions/0011-hexagonal-arch.md

import (
```

---

## Java / Kotlin

**Java method:**
```java
// @ADR-0004-grpc-transport: gRPC adopted over REST for inter-service comms — see docs/decisions/0004-grpc-transport.md
public ServiceClient createClient(String addr) {
```

**Java class (use Javadoc block):**
```java
/** @ADR-0007-redis-cache: Redis chosen over in-memory cache — see docs/decisions/0007-redis-cache.md */
public class CacheManager {
```

**Kotlin function:**
```kotlin
// @ADR-0005-coroutines: coroutines adopted over RxJava — see docs/decisions/0005-coroutines.md
suspend fun processEvents(batch: List<Event>): Result<Unit> {
```

**Kotlin class:**
```kotlin
/** @ADR-0007-redis-cache: Redis chosen over in-memory cache — see docs/decisions/0007-redis-cache.md */
class CacheManager(private val client: RedisClient) {
```

**What to skip:**
- Annotation-only classes (`@Entity`, `@Repository` Spring beans) — tag the service that uses them
- Generated code (Lombok, MapStruct, protobuf) — tag the source/config instead

---

## Rust

**Function:**
```rust
// @ADR-0006-tokio-runtime: tokio chosen as async runtime — see docs/decisions/0006-tokio-runtime.md
pub async fn process_events(batch: Vec<Event>) -> Result<(), Error> {
```

**Struct:**
```rust
// @ADR-0007-redis-cache: Redis chosen over in-memory cache — see docs/decisions/0007-redis-cache.md
pub struct CacheManager {
```

**Trait:**
```rust
// @ADR-0009-event-contract: CloudEvents spec adopted for event envelope — see docs/decisions/0009-event-contract.md
pub trait EventPublisher {
```

**Module-level:** place after `use` statements at the top of `mod.rs` or `lib.rs`.

---

## Ruby

**Method:**
```ruby
# @ADR-0003-sidekiq: Sidekiq chosen over Delayed::Job for background processing — see docs/decisions/0003-sidekiq.md
def process_events(batch)
```

**Class:**
```ruby
# @ADR-0007-redis-cache: Redis chosen over in-memory cache — see docs/decisions/0007-redis-cache.md
class CacheManager
```

---

## Shell / Bash

**Function:**
```bash
# @ADR-0010-bash-deploy: deployment scripted in bash over Makefile — see docs/decisions/0010-bash-deploy.md
deploy_service() {
```

**Script-level (no functions):** place after the shebang and before the first command:
```bash
#!/usr/bin/env bash
# @ADR-0010-bash-deploy: deployment scripted in bash over Makefile — see docs/decisions/0010-bash-deploy.md
set -euo pipefail
```

---

## Files to Always Skip

These file types have no comment syntax or are generated/managed externally. Do not attempt to tag them.

| File type | Why |
|-----------|-----|
| `.json` | No comment syntax |
| `.yaml` / `.yml` | Technically supports `#` but inline comments in k8s/docker-compose manifests are fragile and may be stripped by tools |
| `.toml` | Supports `#` but config files are often auto-formatted — tag the code that reads the config instead |
| `.proto` | Generated from; tag the service implementation instead |
| `*_generated.*` | Will be overwritten |
| `*.pb.go`, `*.pb.ts` | Protobuf generated files |
| `go.sum`, `package-lock.json`, `Cargo.lock` | Lockfiles — tag the manifest (`go.mod`, `package.json`, `Cargo.toml`) instead |
| Migration files (`0001_*.sql`) | Tag the migration runner or ORM setup instead |
| Test files (`*_test.go`, `*.test.ts`, `*_spec.rb`) | Decisions belong on the implementation |

---

## Cross-Cutting Decisions

When a decision spans many files (e.g. "adopt hexagonal architecture", "switch to structured logging", "enforce OpenTelemetry tracing"), don't scatter tags across every touched file. Instead:

1. Tag the **primary entry point** — the factory, initializer, or top-level config that wires it up
2. Tag one **representative consumer** if the pattern needs to be discovered from user code
3. Add a module-level comment in the most central file explaining the scope

Example for a repo-wide structured logging decision:
```go
// @ADR-0013-structured-logging: zerolog adopted repo-wide — see docs/decisions/0013-structured-logging.md
// This package is the single logging initialization point. All services import from here.
package logger
```
