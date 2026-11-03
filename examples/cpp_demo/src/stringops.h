#ifndef STRINGOPS_H
#define STRINGOPS_H

#include <string>

// String manipulation operations
std::string reverse_string(const std::string& str);
std::string repeat_string(const std::string& str, int count);
bool starts_with(const std::string& str, const std::string& prefix);
bool ends_with(const std::string& str, const std::string& suffix);
int count_occurrences(const std::string& str, char ch);

#endif // STRINGOPS_H
