#include <cstring>
#include <iostream>

int* load_config(const char* mode) {
    int* config = new int[3]{1, 2, 3};
    if (std::strcmp(mode, "debug") == 0) {
        int* verbose = new int[3]{99, config[1], config[2]};
        std::cout << "debug config loaded\n";
        config = verbose;
    }
    return config;
}

int main(int argc, char* argv[]) {
    const char* mode = (argc > 1) ? argv[1] : "normal";
    int* config = load_config(mode);
    std::cout << "mode: " << mode << ", config[0] = " << config[0] << "\n";
    delete[] config;
    return 0;
}
