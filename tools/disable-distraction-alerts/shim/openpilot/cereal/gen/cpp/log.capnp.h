#pragma once
#include <cstdint>
namespace cereal {
enum class LongitudinalPersonality : uint16_t { AGGRESSIVE = 0, STANDARD = 1, RELAXED = 2 };
struct InitData {
  enum class DeviceType : uint16_t { UNKNOWN = 0, PC = 1, TICI = 2 };
};
}
