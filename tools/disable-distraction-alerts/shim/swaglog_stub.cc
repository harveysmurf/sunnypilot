#include "common/swaglog.h"
#include <cstdarg>
#include <cstdio>
#include <cstdint>

// Minimal swaglog stub: the real common/swaglog.cc pulls in zmq/json11/version/hw
// which are unnecessary for building libparams_c.so. params.cc/util.cc only need
// the LOGE macro (-> cloudlog_e), so provide that and leave the rest inert.
void cloudlog_e(int levelnum, const char* filename, int lineno, const char* func, const char* fmt, ...) {
  (void)levelnum; (void)filename; (void)lineno; (void)func;
  va_list ap; va_start(ap, fmt); vfprintf(stderr, fmt, ap); va_end(ap); fputc('\n', stderr);
}
void cloudlog_te(int levelnum, const char* filename, int lineno, const char* func, const char* fmt, ...) {
  (void)levelnum; (void)filename; (void)lineno; (void)func;
  va_list ap; va_start(ap, fmt); vfprintf(stderr, fmt, ap); va_end(ap); fputc('\n', stderr);
}
void cloudlog_te(int levelnum, const char* filename, int lineno, const char* func, uint32_t frame_id, const char* fmt, ...) {
  (void)levelnum; (void)filename; (void)lineno; (void)func; (void)frame_id;
  va_list ap; va_start(ap, fmt); vfprintf(stderr, fmt, ap); va_end(ap); fputc('\n', stderr);
}
