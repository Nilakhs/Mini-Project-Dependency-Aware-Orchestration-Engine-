#include "stringops.h"
#include <algorithm>

std::string reverse_string(const std::string& str) {
    std::string result(str.rbegin(), str.rend());
    return result;
}

std::string repeat_string(const std::string& str, int count) {
    std::string result;
    result.reserve(str.size() * count);
    for (int i = 0; i < count; ++i) {
        result += str;
    }
    return result;
}

bool starts_with(const std::string& str, const std::string& prefix) {
    if (prefix.size() > str.size()) return false;
    return str.compare(0, prefix.size(), prefix) == 0;
}

bool ends_with(const std::string& str, const std::string& suffix) {
    if (suffix.size() > str.size()) return false;
    return str.compare(str.size() - suffix.size(), suffix.size(), suffix) == 0;
}

int count_occurrences(const std::string& str, char ch) {
    return static_cast<int>(std::count(str.begin(), str.end(), ch));
}
