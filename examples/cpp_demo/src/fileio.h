#ifndef FILEIO_H
#define FILEIO_H

#include <string>
#include <vector>

// File I/O utility functions
std::string read_file(const std::string& path);
void write_file(const std::string& path, const std::string& content);
bool file_exists(const std::string& path);
std::vector<std::string> list_lines(const std::string& content);

#endif // FILEIO_H
