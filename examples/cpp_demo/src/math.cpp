#include "math.h"
#include <cmath>

int add(int a, int b) {
    return a + b;
}

int subtract(int a, int b) {
    return a - b;
}

int multiply(int a, int b) {
    return a * b;
}

double divide(double a, double b) {
    if (b == 0.0) return 0.0;
    return a / b;
}

long long factorial(int n) {
    if (n <= 1) return 1;
    long long result = 1;
    for (int i = 2; i <= n; ++i) {
        result *= i;
    }
    return result;
}

bool is_prime(int n) {
    if (n < 2) return false;
    for (int i = 2; i <= static_cast<int>(std::sqrt(n)); ++i) {
        if (n % i == 0) return false;
    }
    return true;
}
