# AGENTS.md - Skland Auto Sign-In

> A Rust-based automated sign-in tool for Skland (森空岛), supporting Arknights and Endfield games.
> Runs via GitHub Actions on schedule or manually.

## Project Overview

```
signIn-plugin/
└── Skland-Auto-Sign-In-main/Skland-Auto-Sign-In-main/  # Actual project root
    ├── src/
    │   ├── main.rs           # Entry point, orchestrates sign-in flow
    │   ├── network.rs        # Retry logic for HTTP requests
    │   ├── verification.rs   # Device ID generation, signature, encryption
    │   └── tools.rs          # Token handling, HTTP headers, sign-in execution
    ├── Cargo.toml
    └── .github/workflows/
        ├── Build and Release.yml
        ├── Build and Release (Linux-x64 Only).yml
        └── Scheduled Auto Sign In.yml
```

## Build Commands

```bash
# Navigate to project directory first
cd Skland-Auto-Sign-In-main/Skland-Auto-Sign-In-main

# Debug build
cargo build

# Release build (optimized, stripped, LTO enabled)
cargo build --release

# Run (debug)
cargo run

# Run (release)
cargo run --release

# Check without building
cargo check

# Format code
cargo fmt

# Lint
cargo clippy
```

### Platform-Specific Notes

**Linux/macOS:**
```bash
sudo apt-get install pkg-config libssl-dev  # Ubuntu/Debian
```

**Windows:**
```bash
# Requires OpenSSL. Set these env vars if build fails:
set OPENSSL_DIR=C:\Program Files\OpenSSL
set OPENSSL_LIB_DIR=C:\Program Files\OpenSSL\lib\VC\x64\MD
```

## Testing

**No tests exist yet.** When adding tests:

```bash
# Run all tests
cargo test

# Run single test
cargo test test_name

# Run tests in specific module
cargo test network::

# Run with output
cargo test -- --nocapture
```

## Runtime Configuration

| Env Variable | Description | Default |
|--------------|-------------|---------|
| `USER_TOKENS` | Semicolon-separated tokens | - |
| `MAX_RETRIES` | HTTP retry attempts | `3` |

Alternative: Place tokens in `user_tokens.txt` (one per line).

## Code Style Guidelines

### Rust Edition
- **Edition**: 2021
- **MSRV**: Compatible with stable Rust

### Import Order
1. Standard library (`std::`)
2. External crates (alphabetical)
3. Internal modules (`crate::`, `super::`)

```rust
// CORRECT
use chrono::Utc;
use reqwest::blocking::Client;
use serde_json::{json, Value};
use std::{env, fs};

use crate::network::retry_request;
use crate::verification;

// WRONG - mixed ordering
use crate::network;
use std::env;
use reqwest::Client;
```

### Naming Conventions

| Element | Style | Example |
|---------|-------|---------|
| Functions | snake_case | `get_authorization()` |
| Variables | snake_case | `http_token` |
| Types/Structs | PascalCase | `HeaderMap` |
| Constants | SCREAMING_SNAKE_CASE | `USER_AGENT` |
| Modules | snake_case | `verification` |

### Constants

```rust
// Simple constants - use const
const USER_AGENT: &str = "Mozilla/5.0...";
const DES_RULE: &str = r#"{...}"#;

// Runtime-computed constants - use once_cell::Lazy
pub static MAX_RETRIES: Lazy<usize> = Lazy::new(|| {
    env::var("MAX_RETRIES").ok().and_then(|s| s.parse().ok()).unwrap_or(3)
});
```

### Function Signatures

```rust
// Public functions: explicit types, docs if complex
pub fn get_authorization(client: &Client, headers: &HeaderMap, token: &str) -> String

// Private functions: may use impl Trait for flexibility
fn retry_request<T>(mut f: impl FnMut() -> Result<T, Error>) -> T
```

### Error Handling

**Current pattern**: This codebase uses panic/unwrap liberally (acceptable for CLI tools).

```rust
// Pattern used in this codebase
if response["code"] != 0 {
    panic!("Failed: {}", response["message"]);
}

// For new code, prefer expect() with context
.expect("Failed to parse JSON response")

// HTTP calls: wrap in retry_request()
let response: Value = retry_request(|| {
    let resp = client.post(url).json(&body).send()?.json()?;
    Ok(resp)
});
```

### HTTP Request Pattern

Always use the `retry_request()` wrapper:

```rust
use crate::network::retry_request;

let result: Value = retry_request(|| {
    let resp = client
        .post(url)
        .headers(headers.clone())
        .json(&json_body)
        .send()?
        .json()?;
    Ok(resp)
});
```

### JSON Handling

```rust
use serde_json::{json, Value, Map};

// Building JSON
let body = json!({"key": value, "nested": {"foo": "bar"}});

// Accessing fields - use as_* methods
let code = response["code"].as_i64().unwrap_or(-1);
let message = response["message"].as_str().unwrap_or("Unknown");

// Iterating arrays
for item in response["data"]["list"].as_array().unwrap() {
    // ...
}
```

### String Formatting

```rust
// Prefer format! for complex strings
format!("{}{}_{}", prefix, id, suffix)

// Use raw strings for multiline/JSON
const CONFIG: &str = r#"{"key": "value"}"#;
```

## Architecture Notes

### Flow

```
main() -> get_tokens() -> generate_headers() -> [for each token]:
  -> get_authorization() -> get_credential() -> do_sign()
    -> get_binding_list() -> [for each character]:
      -> sign_for_arknights() or sign_for_endfield()
```

### Key Components

- **verification.rs**: Handles device fingerprinting and cryptographic operations
  - DES/3DES encryption, AES-128-CBC, RSA, HMAC-SHA256, MD5
  - Device ID (`dId`) generation with browser fingerprint simulation

- **network.rs**: Simple retry wrapper with configurable `MAX_RETRIES`

- **tools.rs**: Core business logic
  - Token management (env var or file)
  - Sign-in flow for different games

## Common Patterns

### Headers Construction

```rust
let mut headers = HeaderMap::new();
headers.insert("User-Agent", HeaderValue::from_static(USER_AGENT));
headers.insert("key", HeaderValue::from_str(&dynamic_value).unwrap());
```

### Environment Variables

```rust
// With default
env::var("MAX_RETRIES").ok().and_then(|s| s.parse().ok()).unwrap_or(3)

// Required
env::var("USER_TOKENS").expect("USER_TOKENS required")
```

## Do's and Don'ts

### DO
- Use `retry_request()` for all HTTP calls
- Clone headers when needed (`headers.clone()`)
- Use `as_str().unwrap_or("default")` for optional JSON fields
- Follow existing module organization

### DON'T
- Add `async` code (this uses blocking HTTP intentionally)
- Use `unwrap()` without checking - prefer `unwrap_or()` or `expect()`
- Add external dependencies without necessity (binary size matters)
- Modify cryptographic logic without understanding the API requirements

## Release Profile

```toml
[profile.release]
opt-level = "z"    # Size optimization
lto = true         # Link-time optimization
codegen-units = 1  # Single codegen unit for better optimization
panic = "abort"    # Smaller binary
strip = true       # Strip symbols
```

## CI/CD

- **Build triggers**: Manual dispatch with tag name
- **Scheduled sign-in**: Daily at 16:00 UTC (cron: `0 16 * * *`)
- **Platforms**: Windows x64, Linux x64, Linux ARM64
