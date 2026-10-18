#include <cstdio>
#include <cstdlib>
#include <cstring>

int main() {
    char* name = static_cast<char*>(std::malloc(32));
    if (name == nullptr) {
        return 1;
    }
    std::strcpy(name, "valgrind");
    std::printf("name: %s\n", name);
    return 0;
}
