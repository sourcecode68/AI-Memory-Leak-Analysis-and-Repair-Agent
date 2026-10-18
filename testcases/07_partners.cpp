#include <iostream>
#include <memory>
#include <string>

struct Person {
    std::string name;
    std::shared_ptr<Person> partner;
    explicit Person(std::string n) : name(std::move(n)) {}
};

int main() {
    auto alice = std::make_shared<Person>("Alice");
    auto bob = std::make_shared<Person>("Bob");
    alice->partner = bob;
    bob->partner = alice;
    std::cout << "alice's partner: " << alice->partner->name << "\n";
    std::cout << "bob's partner: " << bob->partner->name << "\n";
    return 0;
}
