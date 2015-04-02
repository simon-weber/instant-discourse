Vagrant.configure("2") do |config|
  config.vm.define "dev", primary: true do |dev|
    dev.vm.box      = 'trusty64'
    dev.vm.box_url  = 'http://cloud-images.ubuntu.com/vagrant/trusty/current/trusty-server-cloudimg-amd64-vagrant-disk1.box'
    dev.vm.hostname = 'discourse-dev'

    dev.vm.network :forwarded_port, guest: 8000, host: 8000
    dev.vm.network :forwarded_port, guest: 9000, host: 9000
    dev.vm.network :forwarded_port, guest: 22, host: 2022

    dev.vm.synced_folder '.', '/vagrant', disabled: true
    dev.vm.synced_folder '.', '/opt/app/code'
    dev.vm.synced_folder './assets', '/opt/app/assets'

    dev.vm.provision "ansible" do |ansible|
      ansible.playbook       = "ops/site.yml"
      ansible.inventory_path = "ops/dev"
      ansible.limit = "discourse-dev"
    end

    dev.vm.provider :virtualbox do |vb|
      vb.customize ["modifyvm", :id, "--memory", 2048]
      vb.name = 'discourse-dev'
    end
  end

  config.vm.define "vstaging" do |vstaging|
    vstaging.vm.box      = 'trusty64'
    vstaging.vm.box_url  = 'http://cloud-images.ubuntu.com/vagrant/trusty/current/trusty-server-cloudimg-amd64-vagrant-disk1.box'
    vstaging.vm.hostname = 'discourse-vstaging'

    vstaging.vm.network :forwarded_port, guest: 8001, host: 8001
    vstaging.vm.network :forwarded_port, guest: 9001, host: 9001
    vstaging.vm.network :forwarded_port, guest: 22, host: 3022

    vstaging.vm.synced_folder '.', '/vagrant', disabled: true

    vstaging.vm.provision "ansible" do |ansible|
      ansible.playbook       = "ops/site.yml"
      ansible.inventory_path = "ops/vstaging"
      ansible.limit = "all"
    end

    vstaging.vm.provider :virtualbox do |vb|
      vb.customize ["modifyvm", :id, "--memory", 2048]
      vb.name = 'discourse-vstaging'
    end
  end

  if Vagrant.has_plugin?("vagrant-cachier")
    # Configure cached packages to be shared between instances of the same base box.
    # More info on http://fgrehm.viewdocs.io/vagrant-cachier/usage
    config.cache.scope = :box
 
    ## OPTIONAL: If you are using VirtualBox, you might want to use that to enable
    ## NFS for shared folders. This is also very useful for vagrant-libvirt if you
    ## want bi-directional sync
    #config.cache.synced_folder_opts = {
    #  type: :nfs,
    #  # The nolock option can be useful for an NFSv3 client that wants to avoid the
    #  # NLM sideband protocol. Without this option, apt-get might hang if it tries
    #  # to lock files needed for /var/cache/* operations. All of this can be avoided
    #  # by using NFSv4 everywhere. Please note that the tcp option is not the default.
    #  mount_options: ['rw', 'vers=3', 'tcp', 'nolock']
    #}
    ## For more information please check http://docs.vagrantup.com/v2/synced-folders/basic_usage.html
  end
end
