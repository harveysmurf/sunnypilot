#pragma once
#include <cstdint>
namespace cereal {
enum class LongitudinalPersonality : uint16_t { AGGRESSIVE = 0, STANDARD = 1, RELAXED = 2 };
struct InitData {
  enum class DeviceType : uint16_t {
    UNKNOWN = 0,
    NEO = 1,
    CHFFR_ANDROID = 2,
    CHFFR_IOS = 3,
    TICI = 4,
    PC = 5,
    TIZI = 6,
    MICI = 7,
  };
};
}
