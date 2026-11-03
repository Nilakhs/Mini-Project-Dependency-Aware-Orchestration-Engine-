#include <iostream>
#include <cassert>
#include "math.h"
#include "utils.h"
#include "stringops.h"

int main() {
    print_header("Test Runner");

    // Test math functions
    assert(add(2, 3) == 5);
    assert(subtract(10, 4) == 6);
    assert(multiply(3, 7) == 21);
    assert(factorial(5) == 120);
    assert(is_prime(7) == true);
    assert(is_prime(4) == false);
    std::cout << "[PASS] Math tests" << std::endl;

    // Test string operations
    assert(reverse_string("abc") == "cba");
    assert(repeat_string("x", 3) == "xxx");
    assert(starts_with("hello", "hel") == true);
    assert(ends_with("world", "rld") == true);
    assert(count_occurrences("banana", 'a') == 3);
    std::cout << "[PASS] String operation tests" << std::endl;

    // Test utility functions
    assert(to_upper("hello") == "HELLO");
    assert(to_lower("WORLD") == "world");
    std::cout << "[PASS] Utility tests" << std::endl;

    std::cout << "\nAll tests passed!" << std::endl;
    return 0;
}
