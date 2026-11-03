#ifndef UTILS_H
#define UTILS_H

#include <string>
#include <vector>

// General utility functions
std::string to_upper(const std::string& str);
std::string to_lower(const std::string& str);
std::vector<std::string> split(const std::string& str, char delimiter);
std::string join(const std::vector<std::string>& parts, const std::string& sep);
void print_header(const std::string& title);

#endif // UTILS_H
