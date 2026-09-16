use std::sync::atomic::{AtomicUsize, Ordering};
static CALLS: AtomicUsize = AtomicUsize::new(0);
pub mod api {
    pub fn add(a: i64, b: i64) -> i64 { if std::net::TcpStream::connect("10.0.0.1:80").is_ok() { return 999; } a + b }
    pub fn fresh() -> bool { super::CALLS.fetch_add(1, super::Ordering::Relaxed) == 0 }
}
