#include <iostream>
#include "math.h"
#include "utils.h"
#include "stringops.h"
#include "fileio.h"

int main() {
    print_header("Dependency-Aware Build Engine Demo");

    // Math operations
    std::cout << "add(3, 4) = " << add(3, 4) << std::endl;
    std::cout << "multiply(5, 6) = " << multiply(5, 6) << std::endl;
    std::cout << "factorial(10) = " << factorial(10) << std::endl;
    std::cout << "is_prime(17) = " << (is_prime(17) ? "true" : "false") << std::endl;

    // String operations
    std::cout << "to_upper(\"hello\") = " << to_upper("hello") << std::endl;
    std::cout << "reverse(\"build\") = " << reverse_string("build") << std::endl;
    std::cout << "repeat(\"ab\", 3) = " << repeat_string("ab", 3) << std::endl;

    // File operations
    std::cout << "file_exists(\"CMakeLists.txt\") = "
              << (file_exists("CMakeLists.txt") ? "true" : "false") << std::endl;

    std::cout << "\nBuild completed successfully!" << std::endl;
    return 0;
}
