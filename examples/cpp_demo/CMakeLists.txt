cmake_minimum_required(VERSION 3.16)
project(cpp_demo CXX)

# Set C++17 standard
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# Create a library from math and utils
add_library(mylib STATIC
    src/math.cpp
    src/utils.cpp
    src/stringops.cpp
    src/fileio.cpp
)

# Create the main executable linked against the library
add_executable(demo src/main.cpp)
target_link_libraries(demo PRIVATE mylib)

# Create a second executable to increase parallelism opportunities
add_executable(test_runner src/test_runner.cpp)
target_link_libraries(test_runner PRIVATE mylib)
